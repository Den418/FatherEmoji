import os

# Значения можно перекрыть переменными окружения, не трогая код:
# API_ID, API_HASH, BOT_TOKEN, DB_PATH
API_ID      = os.getenv("API_ID", "33120499")
API_HASH    = os.getenv("API_HASH", "98835783a52a878e271c0c7acbc24876")
BOT_TOKEN   = os.getenv("BOT_TOKEN", "7894231044:AAENhRHkRCcHGhmLY6Tg-EXMAUgIQyYI6HE")
SUPER_ADMIN = 7720599904

CHANNEL_URL = "https://t.me/YaktonOfficial"
DB_PATH     = os.getenv("DB_PATH", "bot_users.db")

# Пакетное добавление эмодзи (перекраска): Telegram ограничивает частоту addStickerToSet,
# поэтому после каждых BATCH_SIZE эмодзи бот делает паузу BATCH_PAUSE секунд.
BATCH_SIZE  = 8
BATCH_PAUSE = 280
