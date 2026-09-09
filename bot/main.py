from __future__ import annotations

import asyncio
import logging
import signal

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

    # Periodic catalog refresh + polling
    application.job_queue.run_repeating(lambda ctx: periodic_refresh_catalog(), interval=settings.poll_minutes * 60, first=0)

    async with application:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        poll_task = asyncio.create_task(poll_loop(application))
        await application.start()
        await application.updater.start_polling()
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
