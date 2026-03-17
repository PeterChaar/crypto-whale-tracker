#!/usr/bin/env python3
"""
Crypto Whale Tracker — Telegram Bot
Free users: 3 manual checks/day, teaser info (hidden coin name)
Pro users ($9.99/month USDT): auto-alerts with full details + web dashboard
"""

import os
import sys
import json
import logging
import asyncio
from datetime import datetime, timedelta

import httpx
from supabase import create_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# ── Supabase ─────────────────────────────────────────────────────────────────
SUPABASE_URL = "https://zeyqrpfwcvhtzpwjvpfg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpleXFycGZ3Y3ZodHpwd2p2cGZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzAyNzUyNTYsImV4cCI6MjA4NTg1MTI1Nn0.BNBhmZMbxgLP8uKfW86ZY5gv_2ZBPXAZQITVAv_NqDg"
sb = create_client(SUPABASE_URL, SUPABASE_KEY)


def sync_to_supabase(chat_id: int, is_pro: bool, username: str = "", upgraded_at=None, pro_expires=None):
    """Sync subscriber status to Supabase so the website can check it."""
    try:
        row = {
            "chat_id": chat_id,
            "is_pro": is_pro,
            "username": username,
        }
        if upgraded_at:
            row["upgraded_at"] = upgraded_at
        if pro_expires:
            row["pro_expires"] = pro_expires
        sb.table("subscribers").upsert(row, on_conflict="chat_id").execute()
        log.info(f"Supabase sync: {chat_id} is_pro={is_pro}")
    except Exception as e:
        log.error(f"Supabase sync error: {e}")


# ── Subscriber Management ────────────────────────────────────────────────────
SUBSCRIBERS_FILE = os.path.join(os.path.dirname(__file__), "..", "subscribers.json")
FREE_DAILY_LIMIT = 3
PRO_DURATION_DAYS = 30
ADMIN_CHAT_ID = 8421183029

# ── Auto-Payment Verification (TRON Blockchain) ─────────────────────────────
WALLET_ADDRESS = "TDYjRLZwjpehxSKVphhjDkd54NyzWdCxDY"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONGRID_URL = f"https://api.trongrid.io/v1/accounts/{WALLET_ADDRESS}/transactions/trc20"
PENDING_FILE = os.path.join(os.path.dirname(__file__), "..", "pending_payments.json")
PROCESSED_FILE = os.path.join(os.path.dirname(__file__), "..", "processed_txs.json")
PRO_PRICE = 9.99


def load_subs() -> dict:
    try:
        with open(SUBSCRIBERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_subs(subs: dict):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subs, f, indent=2, default=str)


def get_user(chat_id: int) -> dict:
    subs = load_subs()
    key = str(chat_id)
    today = datetime.utcnow().date().isoformat()
    if key not in subs:
        subs[key] = {"is_pro": False, "alerts_today": 0, "last_reset": today, "username": ""}
        save_subs(subs)
    user = subs[key]
    # Reset daily alerts
    if user.get("last_reset") != today:
        user["alerts_today"] = 0
        user["last_reset"] = today
        subs[key] = user
        save_subs(subs)
    # Admin is always Pro
    if chat_id == ADMIN_CHAT_ID:
        if not user.get("is_pro"):
            user["is_pro"] = True
            subs[key] = user
            save_subs(subs)
        return user
    # Check if Pro subscription expired
    if user.get("is_pro") and user.get("pro_expires"):
        try:
            expires = datetime.fromisoformat(user["pro_expires"])
            if datetime.utcnow() > expires:
                user["is_pro"] = False
                subs[key] = user
                save_subs(subs)
                log.info(f"User {chat_id} Pro expired, downgraded to free")
                sync_to_supabase(chat_id, False, user.get("username", ""))
        except (ValueError, TypeError):
            pass
    return user


def update_user(chat_id: int, data: dict):
    subs = load_subs()
    key = str(chat_id)
    if key in subs:
        subs[key].update(data)
    else:
        subs[key] = data
    save_subs(subs)


def increment_alerts(chat_id: int):
    subs = load_subs()
    key = str(chat_id)
    if key in subs:
        subs[key]["alerts_today"] = subs[key].get("alerts_today", 0) + 1
        save_subs(subs)


def set_pro(chat_id: int, username: str = ""):
    subs = load_subs()
    key = str(chat_id)
    now = datetime.utcnow()
    expires = now + timedelta(days=PRO_DURATION_DAYS)
    subs[key] = {
        "is_pro": True,
        "alerts_today": 0,
        "last_reset": now.date().isoformat(),
        "username": username,
        "upgraded_at": now.isoformat(),
        "pro_expires": expires.isoformat(),
    }
    save_subs(subs)
    log.info(f"User {chat_id} ({username}) upgraded to PRO — expires {expires.date()}")
    sync_to_supabase(chat_id, True, username, now.isoformat(), expires.isoformat())


# ── Pending Payments (FIFO queue) ─────────────────────────────────────────────

def load_pending() -> list:
    try:
        with open(PENDING_FILE, "r") as f:
            data = json.load(f)
            # Migrate old dict format to list
            if isinstance(data, dict):
                return []
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_pending(pending: list):
    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2, default=str)


def add_pending_payment(chat_id: int, username: str = ""):
    pending = load_pending()
    # Don't add duplicate — if user already pending, skip
    for p in pending:
        if p["chat_id"] == chat_id:
            return
    pending.append({
        "chat_id": chat_id,
        "username": username,
        "created_at": datetime.utcnow().isoformat(),
    })
    save_pending(pending)


def remove_pending_payment(chat_id: int):
    pending = load_pending()
    pending = [p for p in pending if p["chat_id"] != chat_id]
    save_pending(pending)


def load_processed_txs() -> set:
    try:
        with open(PROCESSED_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_processed_tx(tx_id: str):
    processed = load_processed_txs()
    processed.add(tx_id)
    proc_list = list(processed)
    if len(proc_list) > 200:
        proc_list = proc_list[-200:]
    with open(PROCESSED_FILE, "w") as f:
        json.dump(proc_list, f)


# ── Blockchain Payment Monitor ───────────────────────────────────────────────

async def check_incoming_payments(bot):
    """Poll TronGrid for new USDT transfers and auto-upgrade oldest pending user."""
    pending = load_pending()
    if not pending:
        return

    # Clean expired pending payments (older than 48h)
    now = datetime.utcnow()
    cleaned = [
        p for p in pending
        if (now - datetime.fromisoformat(p["created_at"])).total_seconds() < 172800
    ]
    if len(cleaned) != len(pending):
        save_pending(cleaned)
        pending = cleaned
    if not pending:
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                TRONGRID_URL,
                params={
                    "only_to": "true",
                    "limit": 20,
                    "contract_address": USDT_CONTRACT,
                },
                timeout=10,
            )
            data = resp.json()
    except Exception as e:
        log.error(f"TronGrid poll error: {e}")
        return

    if not data.get("data"):
        return

    processed = load_processed_txs()

    for tx in data["data"]:
        tx_id = tx.get("transaction_id", "")
        if tx_id in processed:
            continue

        token = tx.get("token_info", {})
        if token.get("address") != USDT_CONTRACT:
            continue

        raw_value = int(tx.get("value", "0"))
        amount_usdt = raw_value / 1_000_000

        # Accept any payment between $9.50 and $10.50 as valid $9.99 payment
        if amount_usdt < 9.50 or amount_usdt > 10.50:
            continue

        # FIFO: upgrade the oldest pending user
        pending = load_pending()
        if not pending:
            continue

        info = pending[0]  # oldest
        chat_id = info["chat_id"]
        username = info.get("username", "")

        # Auto-upgrade
        set_pro(chat_id, username)
        save_processed_tx(tx_id)
        remove_pending_payment(chat_id)

        # Notify user
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 *Payment Confirmed — Welcome to WhaleRadar PRO!*\n\n"
                    f"✅ Received *${amount_usdt:.2f} USDT*\n"
                    "Your account is now PRO for *30 days*.\n\n"
                    "✅ Auto whale alerts — ON\n"
                    "✅ Full token details — ON\n"
                    "✅ Unlimited checks — ON\n"
                    "✅ Web dashboard — Unlocked\n\n"
                    "Use /whales to check whale activity!"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"Couldn't notify user {chat_id}: {e}")

        # Notify admin
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=(
                    f"💰 *New Payment Received!*\n\n"
                    f"User: @{username} (`{chat_id}`)\n"
                    f"Amount: ${amount_usdt:.2f} USDT\n"
                    f"TX: `{tx_id[:20]}...`\n"
                    f"Status: ✅ Auto-upgraded to PRO"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"Couldn't notify admin: {e}")

        log.info(f"✅ AUTO-UPGRADE: {chat_id} paid ${amount_usdt:.2f}, tx={tx_id[:16]}")


async def payment_monitor_loop(bot):
    """Background loop: poll TRON blockchain every 30 seconds."""
    log.info("💳 Payment monitor started — polling every 30s")
    await asyncio.sleep(10)  # Initial delay
    while True:
        try:
            await check_incoming_payments(bot)
        except Exception as e:
            log.error(f"Payment monitor error: {e}")
        await asyncio.sleep(30)


async def post_init(application):
    """Start background payment monitor after bot initialization."""
    asyncio.create_task(payment_monitor_loop(application.bot))


# ── Commands ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_chat.id)
    # Save username
    uname = update.effective_user.username or update.effective_user.first_name or ""
    update_user(update.effective_chat.id, {"username": uname})

    # Handle deep link /start upgrade
    if context.args and context.args[0] == "upgrade":
        await pro(update, context)
        return

    plan = "💎 PRO" if user["is_pro"] else "🆓 Free"
    keyboard = [
        [InlineKeyboardButton("🐋 Whale Alerts", callback_data="cmd_whales")],
        [InlineKeyboardButton("📊 Top Movers", callback_data="cmd_top"),
         InlineKeyboardButton("💰 Check Price", callback_data="cmd_price")],
        [InlineKeyboardButton("⛽ Gas Tracker", callback_data="cmd_gas"),
         InlineKeyboardButton("📈 My Stats", callback_data="cmd_stats")],
        [InlineKeyboardButton("🌐 Open Dashboard", url="https://whaleradar.live/#dashboard")],
    ]
    if not user["is_pro"]:
        keyboard.append([InlineKeyboardButton("💎 Upgrade to PRO — $9.99/mo", callback_data="cmd_pro")])

    await update.message.reply_text(
        f"🐋 *WhaleRadar — Crypto Whale Tracker*\n\n"
        f"Your plan: {plan}\n\n"
        f"Track what whales are buying and selling.\n"
        f"Follow smart money. Stay ahead of the market.\n\n"
        f"{'✅ Auto-alerts are ON' if user['is_pro'] else '⚡ Upgrade to PRO for auto-alerts'}\n\n"
        f"Choose an option:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐋 *WhaleRadar Commands:*\n\n"
        "/start — Main menu\n"
        "/whales — See what whales are trading\n"
        "/top — Top gainers & losers (24h)\n"
        "/price btc — Check any token price\n"
        "/gas — ETH gas prices\n"
        "/stats — Your account info\n"
        "/pro — Upgrade to PRO\n\n"
        "*Free:* 3 checks/day, token names hidden\n"
        "*Pro:* Unlimited + auto-alerts + full details",
        parse_mode="Markdown",
    )


async def whales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show whale transactions. Free = teaser, Pro = full details."""
    chat_id = update.effective_chat.id
    user = get_user(chat_id)

    if not user["is_pro"] and user.get("alerts_today", 0) >= FREE_DAILY_LIMIT:
        keyboard = [[InlineKeyboardButton("💎 Upgrade to PRO", callback_data="cmd_pro")]]
        await update.message.reply_text(
            "⚠️ *Daily limit reached!*\n\n"
            f"You've used all {FREE_DAILY_LIMIT} free checks today.\n\n"
            "🔓 *PRO users get:*\n"
            "• Unlimited checks\n"
            "• Auto-alerts sent to you instantly\n"
            "• Full token names & details\n"
            "• Web dashboard access\n\n"
            "Resets at midnight UTC or upgrade now:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    increment_alerts(chat_id)

    await update.message.reply_text("🔍 Scanning whale transactions...")

    from data.fetcher import get_recent_whales
    txns = await get_recent_whales(limit=5)

    if not txns:
        await update.message.reply_text("No significant whale activity detected right now. Check back soon!")
        return

    if user["is_pro"]:
        # PRO: Full details — show exactly what coin, how much, where
        msg = "🐋 *WHALE ACTIVITY — Full Report*\n\n"
        for i, tx in enumerate(txns, 1):
            direction = "🟢 BUY" if tx["type"] == "buy" else "🔴 SELL"
            msg += (
                f"*{i}. {direction}*\n"
                f"   🪙 Token: *{tx['token']}*\n"
                f"   💰 Volume: *${tx['amount_usd']:,.0f}*\n"
                f"   ⛓ Chain: {tx['chain']}\n"
                f"   🔗 [View Details]({tx['tx_url']})\n\n"
            )
        msg += (
            "💡 _Whales often accumulate before big moves.\n"
            "Follow their trades to spot opportunities early._"
        )
    else:
        # FREE: Teaser — hide token name, show volume & direction
        msg = "🐋 *WHALE ACTIVITY — Preview*\n\n"
        for i, tx in enumerate(txns, 1):
            direction = "🟢 BUY" if tx["type"] == "buy" else "🔴 SELL"
            msg += (
                f"*{i}. {direction}*\n"
                f"   🪙 Token: *????*\n"
                f"   💰 Volume: *${tx['amount_usd']:,.0f}*\n"
                f"   ⛓ Chain: {tx['chain']}\n\n"
            )
        remaining = FREE_DAILY_LIMIT - user.get("alerts_today", 1)
        msg += (
            f"🔒 _Token names hidden on Free plan_\n"
            f"📊 _Checks remaining today: {remaining}_\n\n"
            f"💎 /pro — Upgrade to see full details + get auto-alerts"
        )

    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)


async def top_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from data.fetcher import get_top_movers
    movers = await get_top_movers()

    if not movers:
        await update.message.reply_text("Couldn't fetch market data. Try again shortly.")
        return

    msg = "📊 *Market Movers (24h):*\n\n"
    msg += "🟢 *Top Gainers:*\n"
    for coin in movers["gainers"][:5]:
        msg += f"  *{coin['symbol'].upper()}* — ${coin['price']:,.4f} (*+{coin['change']:.1f}%*)\n"

    msg += "\n🔴 *Top Losers:*\n"
    for coin in movers["losers"][:5]:
        msg += f"  *{coin['symbol'].upper()}* — ${coin['price']:,.4f} (*{coin['change']:.1f}%*)\n"

    msg += "\n💡 _Compare this with whale activity to spot smart money moves_"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/price btc` or `/price ethereum`",
            parse_mode="Markdown",
        )
        return

    symbol = context.args[0].lower()
    from data.fetcher import get_price
    data = await get_price(symbol)

    if not data:
        await update.message.reply_text(f"Couldn't find `{symbol}`. Try the full name (e.g. bitcoin, solana).")
        return

    change_emoji = "🟢" if data["change_24h"] >= 0 else "🔴"
    await update.message.reply_text(
        f"💰 *{data['name']}* ({data['symbol'].upper()})\n\n"
        f"💵 Price: *${data['price']:,.6f}*\n"
        f"📈 24h: {change_emoji} *{data['change_24h']:+.2f}%*\n"
        f"🏦 Market Cap: ${data['market_cap']:,.0f}\n"
        f"📊 Volume 24h: ${data['volume']:,.0f}",
        parse_mode="Markdown",
    )


async def gas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from data.fetcher import get_gas_prices
    gas_data = await get_gas_prices()

    if not gas_data:
        await update.message.reply_text("Couldn't fetch gas prices. Try again.")
        return

    await update.message.reply_text(
        "⛽ *ETH Gas Prices:*\n\n"
        f"🐢 Slow: *{gas_data['slow']}* Gwei\n"
        f"🚶 Standard: *{gas_data['standard']}* Gwei\n"
        f"🚀 Fast: *{gas_data['fast']}* Gwei\n\n"
        f"💡 _Low gas = good time to make transactions_",
        parse_mode="Markdown",
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_chat.id)
    plan = "💎 PRO" if user["is_pro"] else "🆓 Free"
    remaining = "∞" if user["is_pro"] else str(max(0, FREE_DAILY_LIMIT - user.get("alerts_today", 0)))

    msg = (
        f"📊 *Your Account:*\n\n"
        f"Plan: {plan}\n"
        f"Whale checks today: {user.get('alerts_today', 0)}\n"
        f"Remaining: {remaining}\n"
    )
    if user["is_pro"]:
        msg += (
            f"\n✅ Auto-alerts: *ON*\n"
            f"✅ Full token details: *ON*\n"
            f"✅ Web dashboard: *Unlocked*"
        )
    else:
        msg += (
            f"\n❌ Auto-alerts: OFF (PRO only)\n"
            f"❌ Token names: Hidden (PRO only)\n"
            f"❌ Web dashboard: Locked\n\n"
            f"💎 /pro to upgrade"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_chat.id)
    if user["is_pro"]:
        await update.message.reply_text(
            "✅ *You're already on PRO!*\n\n"
            "You have unlimited alerts, auto-notifications, and full dashboard access.",
            parse_mode="Markdown",
        )
        return

    keyboard = [
        [InlineKeyboardButton("💳 Pay with Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton("💰 Send USDT Directly", callback_data="pay_usdt")],
    ]
    await update.message.reply_text(
        "💎 *WhaleRadar PRO — $9.99/month*\n\n"
        "*What you get:*\n"
        "✅ Auto whale alerts — sent to you instantly\n"
        "✅ See exactly which coins whales buy/sell\n"
        "✅ Unlimited whale checks\n"
        "✅ Full web dashboard with charts\n"
        "✅ Advanced wallet tracking\n"
        "✅ Priority notifications\n\n"
        "*Why it matters:*\n"
        "_Whales know things before the market does.\n"
        "When a whale buys $2M of a token, something is happening.\n"
        "PRO users see it first and can act before the crowd._\n\n"
        "Choose payment method:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Callback Handlers ────────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cmd_whales":
        # Create a fake update to call whales command
        await query.message.reply_text("Use /whales to see whale activity")
    elif query.data == "cmd_top":
        await query.message.reply_text("Use /top to see market movers")
    elif query.data == "cmd_price":
        await query.message.reply_text("Use /price <symbol>\nExample: /price btc")
    elif query.data == "cmd_gas":
        await query.message.reply_text("Use /gas to check gas prices")
    elif query.data == "cmd_stats":
        await query.message.reply_text("Use /stats to see your account")
    elif query.data == "cmd_pro":
        await query.message.reply_text("Use /pro to see upgrade options")
    elif query.data == "pay_binance":
        # TODO: Replace with your Binance Pay ID
        await query.message.reply_text(
            "💳 *Binance Pay*\n\n"
            "Send exactly *9.99 USDT* via Binance Pay:\n\n"
            "1️⃣ Open Binance app\n"
            "2️⃣ Tap *Pay* → *Send*\n"
            "3️⃣ Enter Binance ID: `YOUR_BINANCE_ID`\n"
            "4️⃣ Amount: *9.99 USDT*\n"
            "5️⃣ In the note, write your Telegram username\n\n"
            "After payment, send the screenshot here.\n"
            "You'll be upgraded within minutes!",
            parse_mode="Markdown",
        )
    elif query.data == "pay_usdt":
        chat_id = query.from_user.id
        username = query.from_user.username or query.from_user.first_name or ""
        add_pending_payment(chat_id, username)
        await query.message.reply_text(
            "💰 *Direct USDT Transfer*\n\n"
            "Send *$9.99 USDT* to this wallet:\n\n"
            f"`{WALLET_ADDRESS}`\n\n"
            "📋 _Tap the address to copy_\n\n"
            "⚠️ *TRC20 (Tron) network ONLY!*\n\n"
            "Your upgrade will be *automatic* within 1-2 minutes "
            "after the transaction confirms on the blockchain.\n\n"
            "⚠️ Sending on the wrong network = lost funds.",
            parse_mode="Markdown",
        )


# ── Photo handler (for payment screenshots) ─────────────────────────────────

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When user sends a photo (payment screenshot), forward to admin."""
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name or str(chat_id)

    await update.message.reply_text(
        "✅ *Screenshot received!*\n\n"
        "Payments are detected automatically from the blockchain.\n"
        "If you sent the exact unique amount, you'll be upgraded within 1-2 minutes.\n\n"
        "If auto-detection doesn't trigger, our team will verify manually.",
        parse_mode="Markdown",
    )

    # Forward screenshot to admin for verification
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"💳 *New Payment Screenshot*\n\n"
                f"From: @{username}\n"
                f"Chat ID: `{chat_id}`\n\n"
                f"To upgrade, reply:\n`/upgrade {chat_id}`"
            ),
            parse_mode="Markdown",
        )
        await update.message.forward(chat_id=ADMIN_CHAT_ID)
    except Exception as e:
        log.error(f"Failed to forward payment to admin: {e}")

    log.info(f"💳 PAYMENT SCREENSHOT from @{username} (chat_id: {chat_id})")


# ── Admin command to manually upgrade users ──────────────────────────────────

async def admin_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /upgrade <chat_id> to manually upgrade a user."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /upgrade <chat_id>")
        return

    try:
        target_id = int(context.args[0])
        set_pro(target_id, "")
        await update.message.reply_text(f"✅ User {target_id} upgraded to PRO!")

        # Notify the user
        try:
            from telegram import Bot
            bot = Bot(token=BOT_TOKEN)
            await bot.send_message(
                chat_id=target_id,
                text=(
                    "🎉 *Welcome to WhaleRadar PRO!*\n\n"
                    "Your account has been upgraded for *30 days*.\n\n"
                    "✅ Auto whale alerts — coming to you automatically\n"
                    "✅ Full token names & details visible\n"
                    "✅ Unlimited whale checks\n"
                    "✅ Web dashboard unlocked\n\n"
                    "Whale alerts will start arriving automatically.\n"
                    "Use /whales to check anytime!"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"Couldn't notify user {target_id}: {e}")

    except ValueError:
        await update.message.reply_text("Invalid chat_id. Must be a number.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("whales", whales))
    app.add_handler(CommandHandler("top", top_movers))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("gas", gas))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("pro", pro))
    app.add_handler(CommandHandler("upgrade", admin_upgrade))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    log.info("🐋 WhaleRadar Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
