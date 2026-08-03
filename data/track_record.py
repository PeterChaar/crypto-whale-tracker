#!/usr/bin/env python3
"""
Follow-ups: what the price did after a whale move.

An alert on its own asks a stranger to take the product on faith. A follow-up
under that same alert, thirty minutes later, showing what the price actually
did, is the only thing that turns a channel reader into a paying member. It
also keeps the product honest, because the follow-up posts whatever happened,
including the times the move meant nothing.

State lives in a small JSON file. Losing it costs at most a handful of pending
follow-ups, so it deliberately does not reach for the database.
"""

import os
import json
import time
import logging
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PENDING_FILE = ROOT / "pending_followups.json"

FOLLOWUP_MINUTES = float(os.environ.get("WR_FOLLOWUP_MINUTES", 30))
# Only the genuinely large moves earn a follow-up. Every alert getting one
# would bury the channel in its own commentary.
FOLLOWUP_MIN_USD = float(os.environ.get("WR_FOLLOWUP_MIN_USD", 20_000_000))
TRACKABLE = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def _read() -> list:
    try:
        return json.loads(PENDING_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write(rows: list) -> None:
    try:
        PENDING_FILE.write_text(json.dumps(rows, indent=2))
    except OSError as e:
        log.warning(f"could not save follow-ups: {e}")


def trackable(whale: dict) -> bool:
    return (
        whale.get("kind") == "onchain"
        and whale.get("unit") in TRACKABLE
        and whale.get("amount_usd", 0) >= FOLLOWUP_MIN_USD
        and (whale.get("price") or 0) > 0
    )


def remember(whale: dict, message_id: int) -> None:
    """Schedule a follow-up under a channel post we just made."""
    if not trackable(whale) or not message_id:
        return
    rows = _read()
    rows.append({
        "message_id": message_id,
        "symbol": TRACKABLE[whale["unit"]],
        "unit": whale["unit"],
        "amount_usd": whale["amount_usd"],
        "price_at_alert": whale["price"],
        # Stored, not read back from config: if the interval is changed while
        # a follow-up is queued, the post must still say how long it waited.
        "minutes": FOLLOWUP_MINUTES,
        "due_at": time.time() + FOLLOWUP_MINUTES * 60,
    })
    _write(rows)
    log.info(f"follow-up scheduled for message {message_id} in {FOLLOWUP_MINUTES:.0f}m")


def take_due() -> list:
    """Return follow-ups whose time has come, and drop them from the queue."""
    rows = _read()
    if not rows:
        return []
    now = time.time()
    due = [r for r in rows if r.get("due_at", 0) <= now]
    if due:
        _write([r for r in rows if r.get("due_at", 0) > now])
    return due


def format_followup(row: dict, price_now: float) -> str:
    before = row["price_at_alert"]
    if not before:
        return ""
    change = (price_now - before) / before * 100
    unit = row["unit"]

    if change >= 0.5:
        verdict, arrow = "price went UP after it", "\U0001F7E2"
    elif change <= -0.5:
        verdict, arrow = "price went DOWN after it", "\U0001F534"
    else:
        verdict, arrow = "price barely moved", "⚪"

    mins = int(row.get("minutes", FOLLOWUP_MINUTES))
    return (
        f"\U0001F4CA *FOLLOW UP — {mins} min later*\n\n"
        f"{arrow} {unit} {change:+.2f}% since that alert, {verdict}.\n"
        f"${before:,.2f} → ${price_now:,.2f}\n\n"
        f"_Posted whatever happened, including the quiet ones. "
        f"PRO members saw the exchange order flow behind this move as it filled._"
    )
