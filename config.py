import os

# Значения можно перекрыть переменными окружения, не трогая код:
# API_ID, API_HASH, BOT_TOKEN, DB_PATH
API_ID      = os.getenv("API_ID", "33120499")
API_HASH    = os.getenv("API_HASH", "98835783a52a878e271c0c7acbc24876")
BOT_TOKEN   = "7894231044:AAFourm2E-E7h_qt4Ue__waOPvdWISAvPD0"
SUPER_ADMIN = 7720599904

CHANNEL_URL = "https://t.me/YaktonOfficial"
DB_PATH     = os.getenv("DB_PATH", "bot_users.db")

# Пакетное добавление эмодзи (перекраска): Telegram ограничивает частоту addStickerToSet,
# поэтому после каждых BATCH_SIZE эмодзи бот делает паузу BATCH_PAUSE секунд.
BATCH_SIZE  = 8
BATCH_PAUSE = 280
