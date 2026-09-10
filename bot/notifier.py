from __future__ import annotations

import logging

from telegram import Bot
from telegram.constants import ParseMode

from . import token_manager
from .api_client import (
    fetch_all_demos_for_cities,
    fetch_all_demos_for_chains,
    fetch_all_demos_for_orgs,
)
from .database import (
    get_all_subscriptions,
    is_seen,
    mark_seen,
)
from .handlers import build_catalog
from .notifications import format_demo

logger = logging.getLogger(__name__)


async def poll_and_notify(bot: Bot) -> None:
    if not token_manager.is_configured():
        logger.warning("No API token configured; skipping poll")
        return

    subs = await get_all_subscriptions()
    city_subs = [s for s in subs if s["sub_type"] == "city"]
    org_subs = [s for s in subs if s["sub_type"] == "org"]
    chain_subs = [s for s in subs if s["sub_type"] == "chain"]

    # demo_id -> (message_text, set(chat_ids))
    delivery: dict[str, tuple[str, set[int]]] = {}

    def add(demo: dict, chat_id: int) -> None:
        demo_id = str(demo.get("_id") or demo.get("id"))
        if not demo_id:
            return
        if demo_id in delivery:
            delivery[demo_id][1].add(chat_id)
        else:
            delivery[demo_id] = (format_demo(demo), {chat_id})

    # ── Cities: demo matches iff its city is subscribed ──
    subscribed_cities = sorted({s["sub_key"] for s in city_subs})
    if subscribed_cities:
        try:
            city_demos = await fetch_all_demos_for_cities(subscribed_cities)
            city_key_set = {s["sub_key"].casefold(): s["chat_id"] for s in city_subs}
            for demo in city_demos:
                city = (demo.get("city") or "").casefold()
                for sub in city_subs:
                    if city == sub["sub_key"].casefold():
                        add(demo, sub["chat_id"])
        except Exception:
            logger.exception("City demo fetch failed")

    # ── Orgs: each demo fetched for a specific org belongs 100% to it ──
    for sub in org_subs:
        try:
            demos = await fetch_all_demos_for_orgs([sub["sub_key"]])
            for demo in demos:
                add(demo, sub["chat_id"])
        except Exception:
            logger.exception("Org demo fetch failed")

    # ── Chains: each demo fetched for a parent belongs 100% to that chain ──
    for sub in chain_subs:
        try:
            demos = await fetch_all_demos_for_chains([sub["sub_key"]])
            for demo in demos:
                add(demo, sub["chat_id"])
        except Exception:
            logger.exception("Chain demo fetch failed")

    sent = 0
    for demo_id, (text, chat_ids) in delivery.items():
        for chat_id in chat_ids:
            if await is_seen(chat_id, demo_id):
                continue
            try:
                await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                await mark_seen(chat_id, demo_id)
                sent += 1
            except Exception:
                logger.exception(f"Failed to send notification to {chat_id} for {demo_id}")

    logger.info("Poll finished; sent %d notifications", sent)


async def periodic_refresh_catalog() -> None:
    try:
        await build_catalog()
    except Exception:
        logger.exception("Periodic catalog refresh failed")
