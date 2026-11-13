"""Single-run Telegram gift monitor for scheduled GitHub Actions.

This module deliberately does not import GiftMonitor or its auto-DM controls.
It only reads public gift metadata with a user MTProto session and sends
notifications to the explicitly configured admin chat via Bot API.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from telethon import errors
from telethon.sessions import StringSession

from .models import Collection, GiftEvent
from .notifier import BotNotifier
from .storage import Storage
from .telegram_api import GiftTelegramApi

LOG = logging.getLogger("gift_tracking.actions_monitor")


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing GitHub Actions secret: {name}")
    return value


def integer(name: str, default: int, minimum: int = 0) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def get_state(db: Storage, key: str, default: int = 0) -> int:
    row = db.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return int(row["value"]) if row else default


def set_state(db: Storage, key: str, value: int) -> None:
    db.connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (key, str(value)),
    )
    db.connection.commit()


async def flush_pending(db: Storage, notifier: BotNotifier) -> int:
    delivered = 0
    for event in db.pending_notifications(limit=100):
        # Messages only go to NOTIFY_CHAT_ID. There are no owner-message calls.
        await notifier.send_event(event)
        db.mark_notified(event.slug)
        delivered += 1
    return delivered


async def discover(
    api: GiftTelegramApi, db: Storage, *, max_checks: int, deadline: float
) -> None:
    candidates = await api.catalog()
    if not candidates:
        LOG.warning("Telegram returned no catalog candidates")
        return
    known_ids = {item.gift_id for item in db.list_collections()}
    cursor = get_state(db, "actions_discovery_cursor") % len(candidates)
    attempted = 0
    traversed = 0
    while traversed < len(candidates) and attempted < max_checks:
        if time.monotonic() >= deadline:
            break
        candidate = candidates[cursor]
        cursor = (cursor + 1) % len(candidates)
        traversed += 1
        set_state(db, "actions_discovery_cursor", cursor)
        if candidate.gift_id in known_ids:
            continue
        attempted += 1
        try:
            anchor, _ = await api.unique_gift(f"{candidate.slug_prefix}-1")
        except (errors.RPCError, TypeError) as exc:
            LOG.info("Not a unique collection yet: %s (%s)", candidate.title, exc)
            continue
        prefix = anchor.slug.rsplit("-", 1)[0]
        db.upsert_collection(Collection(candidate.gift_id, candidate.title, prefix))
        known_ids.add(candidate.gift_id)
        LOG.info("Discovered %s", prefix)


async def scan_one(
    api: GiftTelegramApi, db: Storage, collection: Collection, *, limit: int
) -> None:
    anchor, _ = await api.unique_gift(f"{collection.slug_prefix}-1")
    issued = anchor.availability_issued
    if issued is None:
        return
    if collection.last_issued is None:
        # Do not alert about historical gifts on first observation.
        db.set_last_issued(collection.gift_id, issued)
        LOG.info("Baselined %s at #%s", collection.slug_prefix, issued)
        return
    if issued <= collection.last_issued:
        return
    for number in range(collection.last_issued + 1, min(issued, collection.last_issued + limit) + 1):
        slug = f"{collection.slug_prefix}-{number}"
        try:
            event, _ = await api.unique_gift(slug)
        except (errors.RPCError, TypeError) as exc:
            attempts = db.increment_missing_gift_attempt(collection.gift_id, slug, number)
            if attempts < 3:
                LOG.warning("Gift %s not available yet (%s/3): %s", slug, attempts, exc)
                return
            LOG.warning("Skipping unavailable gift %s after 3 attempts", slug)
            db.clear_missing_gift(slug)
            db.set_last_issued(collection.gift_id, number)
            continue
        db.clear_missing_gift(slug)
        if db.record_gift(event):
            LOG.info("New gift: %s owner=@%s", event.slug, event.owner_username or "unknown")
        db.set_last_issued(collection.gift_id, number)


async def scan(
    api: GiftTelegramApi,
    db: Storage,
    *,
    deadline: float,
    max_new_per_collection: int,
) -> int:
    collections = db.list_collections()
    if not collections:
        LOG.warning("No unique gift collections found yet")
        return 0
    cursor = get_state(db, "actions_scan_cursor") % len(collections)
    checked = 0
    while checked < len(collections) and time.monotonic() < deadline:
        item = collections[cursor]
        cursor = (cursor + 1) % len(collections)
        checked += 1
        try:
            await scan_one(api, db, item, limit=max_new_per_collection)
        except (errors.RPCError, TypeError, TimeoutError, OSError) as exc:
            LOG.warning("Error checking %s: %s", item.slug_prefix, exc)
        finally:
            set_state(db, "actions_scan_cursor", cursor)
    return checked


async def run_once() -> None:
    from .config import ConfigError

    api_id = int(required("TG_API_ID"))
    api_hash = required("TG_API_HASH")
    session_secret = required("TG_SESSION_STRING")
    token = required("BOT_TOKEN")
    chat_id = required("NOTIFY_CHAT_ID")
    path = Path(os.environ.get("DATABASE_PATH", ".state/gift_tracking.sqlite3"))
    budget_seconds = integer("RUN_BUDGET_SECONDS", 210, 30)
    discovery_checks = integer("DISCOVERY_ATTEMPTS", 20, 1)
    max_new = integer("MAX_NEW_GIFTS_PER_CHECK", 100, 1)
    timezone = os.environ.get("TIMEZONE", "Europe/Kyiv")

    deadline = time.monotonic() + budget_seconds
    db = Storage(path)
    api = GiftTelegramApi(api_id, api_hash, StringSession(session_secret))
    notifier = BotNotifier(token, chat_id, timezone)
    try:
        await api.connect()
        if not await api.is_authorized():
            raise RuntimeError("TG_SESSION_STRING is not authorized. Create a new session locally.")
        if get_state(db, "actions_start_alert_sent") == 0:
            await notifier.send_text(
                "✅ <b>Gift Tracking Actions увімкнено.</b> "
                "Перший запуск запам'ятовує поточні номери колекцій; "
                "далі повідомлятиму про нові."
            )
            set_state(db, "actions_start_alert_sent", 1)
        sent_before = await flush_pending(db, notifier)
        await discover(api, db, max_checks=discovery_checks, deadline=deadline)
        count = await scan(api, db, deadline=deadline, max_new_per_collection=max_new)
        sent_after = await flush_pending(db, notifier)
        LOG.info("Checked %s collections; sent %s alerts", count, sent_before + sent_after)
    finally:
        await api.disconnect()
        db.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
