from __future__ import annotations

import asyncio
import logging
import signal

from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder

from . import token_manager
from .config import settings
from .database import init_db
from .handlers import build_handlers, build_catalog, set_admin_from_env
from .notifier import poll_and_notify, periodic_refresh_catalog

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def poll_loop(application) -> None:
    bot = application.bot
    while True:
        await poll_and_notify(bot)
        await asyncio.sleep(settings.poll_minutes * 60)


async def handle_error(update, context) -> None:
    if isinstance(context.error, BadRequest):
        message = str(context.error.message or "")
        if "message is not modified" in message:
            return
        if "query is too old" in message.lower() or "button_data_invalid" in message:
            return
    logger.error("Unhandled handler error", exc_info=context.error)


async def run() -> None:
    await init_db(settings.db_path)
    await set_admin_from_env(settings.admin_chat_ids)
    if token_manager.is_configured():
        try:
            await build_catalog()
        except Exception:
            logger.exception("Initial catalog build failed")

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .build()
    )
    build_handlers(application)
    application.add_error_handler(handle_error)

    # Periodic catalog refresh + polling
    application.job_queue.run_repeating(lambda ctx: periodic_refresh_catalog(), interval=settings.poll_minutes * 60, first=0)

    async with application:
        # Register slash commands with Telegram
        await application.bot.set_my_commands([
            ("start", "Käynnistä botti"),
            ("tilaa", "Hallitse tilauksia"),
            ("listaa", "Listaa tulevat mielenosoitukset"),
            ("tanaan", "Tänään tapahtuvat"),
            ("viikolla", "Tällä viikolla"),
            ("menu", "Avaa päävalikko"),
            ("ryhma", "Liitä ryhmä/kanava DM:ään"),
            ("liita", "Liitä koodilla"),
            ("hallinta", "Hallitse liitetyt ryhmät/kanavat"),
            ("config", "Aseta API-token (ylläpitäjä)"),
            ("paivita", "Päivitä katalogi (ylläpitäjä)"),
            ("status", "Tarkista botin tila"),
            ("ohjeet", "Näytä ohjeet"),
        ])
        logger.info("Slash commands registered with Telegram")

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        poll_task = asyncio.create_task(poll_loop(application))
        await application.start()
        await application.updater.start_polling(allowed_updates=[
            "message", "edited_message", "channel_post", "edited_channel_post",
            "callback_query", "my_chat_member", "chat_member",
        ])
        try:
            await stop.wait()
        finally:
            poll_task.cancel()
            await application.updater.stop()
            await application.stop()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
