import os

# Значения можно перекрыть переменными окружения, не трогая код:
# API_ID, API_HASH, BOT_TOKEN, DB_PATH
API_ID      = os.getenv("API_ID", "33120499")
API_HASH    = os.getenv("API_HASH", "98835783a52a878e271c0c7acbc24876")
BOT_TOKEN   = os.getenv("BOT_TOKEN", "8981721906:AAFZsT5bIGYnPwFRLZi5g0dDc0ax_a-3YnE")
SUPER_ADMIN = 7720599904

CHANNEL_URL = "https://t.me/YaktonOfficial"
DB_PATH     = os.getenv("DB_PATH", "bot_users.db")
