"""Durable state for WhaleRadar.

Subscribers, pending payments and processed transactions all used to live in
three JSON files next to the code. On the Air that is one deleted file from
gone; on Railway, whose filesystem is ephemeral, every single redeploy wiped
them. Either way a paying customer silently lost Pro and, worse, the last 20
TRC20 transfers on the wallet looked unprocessed again and got re-credited to
whoever happened to be first in the pending queue.

So Postgres is the source of truth and the JSON files are only a warm cache.
The bot reads the cache (fast, no network on the hot path) and writes through
to Postgres on every change, and rehydrates the cache from Postgres at boot.

The money path is deliberately different from the rest: crediting a payment
requires an exclusive claim on the transaction id, and if the database cannot
be reached the payment is NOT credited. Refusing to credit is recoverable, a
customer messages you and you fix it. Crediting twice is not.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SUBSCRIBERS_FILE = ROOT / "subscribers.json"
PENDING_FILE = ROOT / "pending_payments.json"
PROCESSED_FILE = ROOT / "processed_txs.json"

T_SUBS = "whaleradar_subscribers"
T_PENDING = "whaleradar_pending"
T_TX = "whaleradar_processed_tx"

_sb = None
_resolved = False


def _client():
    """Connect on first use, not at import.

    whale_bot.py imports this module about twenty lines before it calls
    load_dotenv, so reading os.environ at import time saw an empty environment
    and would have left the bot permanently degraded while looking configured.
    Resolving lazily means import order cannot break it.

    Config is repointable in one env var. The tables currently live in the
    selfie2id project because WhaleRadar's own Supabase is paused and the org
    is at its 2 active free project limit; restore zeyqrpfwcvhtzpwjvpfg and
    change these to move back.
    """
    global _sb, _resolved
    if _resolved:
        return _sb
    _resolved = True

    try:  # harmless if the caller already did it
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    url = os.environ.get("WHALERADAR_SUPABASE_URL", "").strip()
    # Must be the SERVICE ROLE key. The tables have RLS on with no policies, so
    # the anon key that ships in the public site bundle cannot grant Pro.
    key = os.environ.get("WHALERADAR_SUPABASE_KEY", "").strip()
    if not url or not key:
        log.error(
            "WHALERADAR_SUPABASE_URL / WHALERADAR_SUPABASE_KEY are not set. "
            "Subscriber state is local-disk only, so it is one lost file (or one "
            "Railway redeploy) from gone, and payments will NOT be auto-credited "
            "until these are set."
        )
        return None

    try:
        from supabase import create_client

        _sb = create_client(url, key)
    except Exception as e:  # pragma: no cover - depends on deploy env
        log.error("SUPABASE INIT FAILED, running on local storage only: %s", e)
        _sb = None
    return _sb


def set_client(client) -> None:
    """Inject a client. Used by test_redeploy.py."""
    global _sb, _resolved
    _sb, _resolved = client, True


def durable() -> bool:
    """True when state survives losing the local files."""
    return _client() is not None


# ── local cache ───────────────────────────────────────────────────────────────

def _read(path: Path, empty):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return empty


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


# ── subscribers ───────────────────────────────────────────────────────────────

def load_subs() -> dict:
    return _read(SUBSCRIBERS_FILE, {})


def save_subs(subs: dict) -> None:
    _write(SUBSCRIBERS_FILE, subs)


def push_sub(chat_id: int, user: dict) -> None:
    """Write one subscriber through to Postgres. Never raises."""
    sb = _client()
    if not sb:
        return
    row = {
        "chat_id": int(chat_id),
        "is_pro": bool(user.get("is_pro", False)),
        "username": user.get("username") or "",
        "alerts_today": int(user.get("alerts_today", 0) or 0),
        "last_reset": user.get("last_reset") or None,
        "upgraded_at": user.get("upgraded_at") or None,
        "pro_expires": user.get("pro_expires") or None,
    }
    if user.get("auth_token"):
        row["auth_token"] = user["auth_token"]
    try:
        sb.table(T_SUBS).upsert(row, on_conflict="chat_id").execute()
    except Exception as e:
        log.error("subscriber write-through failed for %s: %s", chat_id, e)


# ── pending payments ──────────────────────────────────────────────────────────

def load_pending() -> list:
    data = _read(PENDING_FILE, [])
    return [] if isinstance(data, dict) else data  # migrate the old dict shape


def save_pending(pending: list) -> None:
    _write(PENDING_FILE, pending)


def push_pending(chat_id: int, username: str, created_at: str) -> None:
    sb = _client()
    if not sb:
        return
    try:
        sb.table(T_PENDING).upsert(
            {"chat_id": int(chat_id), "username": username or "", "created_at": created_at},
            on_conflict="chat_id",
        ).execute()
    except Exception as e:
        log.error("pending write-through failed for %s: %s", chat_id, e)


def drop_pending(chat_id: int) -> None:
    sb = _client()
    if not sb:
        return
    try:
        sb.table(T_PENDING).delete().eq("chat_id", int(chat_id)).execute()
    except Exception as e:
        log.error("pending delete failed for %s: %s", chat_id, e)


# ── processed transactions, the money path ────────────────────────────────────

def claim_tx(tx_id: str, chat_id: int, amount: float) -> bool:
    """Take exclusive ownership of a transaction id. True means credit it.

    This is an INSERT, not an upsert, against a primary key. Two pollers racing
    on the same transfer, or one poller restarting mid-credit, produce a
    duplicate-key error on the second attempt rather than a second 30 days of
    Pro. A False return always means "already handled, or not safe to handle".
    """
    sb = _client()
    if not sb:
        log.error("REFUSING to credit tx %s: no durable store, cannot rule out a repeat", tx_id)
        return False
    try:
        sb.table(T_TX).insert(
            {"tx_id": tx_id, "chat_id": int(chat_id), "amount_usdt": round(float(amount), 2)}
        ).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "23505" in str(e):
            log.info("tx %s already credited, skipping", tx_id)
        else:
            log.error("REFUSING to credit tx %s: claim failed: %s", tx_id, e)
        return False


def load_processed_txs() -> set:
    """Cheap pre-filter only. claim_tx is what actually guarantees uniqueness."""
    return set(_read(PROCESSED_FILE, []))


def cache_processed_tx(tx_id: str) -> None:
    txs = list(load_processed_txs() | {tx_id})[-200:]
    _write(PROCESSED_FILE, txs)


# ── boot ──────────────────────────────────────────────────────────────────────

def rehydrate() -> None:
    """Rebuild the local cache from Postgres. Safe to call on every start."""
    sb = _client()
    if not sb:
        log.warning("no durable store; keeping whatever is on local disk")
        return
    try:
        subs = {}
        for r in (sb.table(T_SUBS).select("*").execute().data or []):
            subs[str(r["chat_id"])] = {
                "is_pro": r.get("is_pro", False),
                "alerts_today": r.get("alerts_today", 0),
                "last_reset": r.get("last_reset"),
                "username": r.get("username", ""),
                "upgraded_at": r.get("upgraded_at"),
                "pro_expires": r.get("pro_expires"),
                "auth_token": r.get("auth_token"),
            }
        save_subs(subs)

        pending = sb.table(T_PENDING).select("*").order("created_at").execute().data or []
        save_pending(
            [
                {"chat_id": p["chat_id"], "username": p.get("username", ""),
                 "created_at": str(p["created_at"])}
                for p in pending
            ]
        )

        txs = (
            sb.table(T_TX).select("tx_id").order("processed_at", desc=True)
            .limit(200).execute().data or []
        )
        _write(PROCESSED_FILE, [t["tx_id"] for t in txs])

        pro = sum(1 for u in subs.values() if u.get("is_pro"))
        log.info(
            "rehydrated from Postgres: %d subscribers (%d Pro), %d pending, %d known txs",
            len(subs), pro, len(pending), len(txs),
        )
    except Exception as e:
        log.error("REHYDRATE FAILED, local cache may be stale: %s", e)
