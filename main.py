import logging
import os

from hydrogram import idle

import handlers  # noqa: F401 — импорт регистрирует все обработчики
import tg
from db import init_db
from tg import app, log


async def start_bot():
    await app.start()

    os.makedirs("temp", exist_ok=True)

    tg.bot_username = (await app.get_me()).username
    log.info(f"Бот @{tg.bot_username} запущен и готов к работе.")

    await idle()
    await app.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    log.info("База данных инициализирована.")
    app.run(start_bot())
