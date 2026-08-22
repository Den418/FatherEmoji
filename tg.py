import asyncio
import html as html_module
import json
import logging
import re

import requests
from hydrogram import Client, enums

import config
import db
import texts as T

log = logging.getLogger("fatheremoji")

app = Client("emoji_pro_bot", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

# Заполняется при старте бота (main.start_bot).
bot_username = ""


def h(text) -> str:
    """Экранирование пользовательского текста для HTML-parse mode."""
    return html_module.escape(str(text))


def bot_suffix() -> str:
    return f"_by_{bot_username}"


def pack_link(name: str) -> str:
    """Готовая HTML-ссылка «Открыть пак» для сообщений."""
    return f'<a href="https://t.me/addemoji/{h(name)}">{T.IB_OPEN}</a>'


def sanitize_link(raw: str) -> str:
    for marker in ("addemoji/", "addstickers/"):
        if marker in raw:
            raw = raw.split(marker)[-1]
    raw = raw.strip()
    raw = re.sub(r"[^a-zA-Z0-9_]", "", raw)

    suffix = bot_suffix()
    if not raw.lower().endswith(suffix.lower()):
        if "_by_" in raw.lower():
            raw = re.sub(r"(?i)_by_.*$", suffix, raw)
        else:
            raw += suffix
    return raw


async def tg_api(method: str, data: dict = None, files: dict = None) -> dict:
    def _req():
        url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
        try:
            return requests.post(url, data=data, files=files, timeout=60).json()
        except Exception as e:
            log.error(f"tg_api {method} request error: {e}")
            return {}
        finally:
            if files:
                for tuple_val in files.values():
                    # (filename, fileobj, mimetype) или просто fileobj
                    if isinstance(tuple_val, tuple) and len(tuple_val) > 1:
                        f_obj = tuple_val[1]
                    else:
                        f_obj = tuple_val
                    if hasattr(f_obj, "close"):
                        f_obj.close()
    return await asyncio.to_thread(_req)


async def tg_api_retry(method: str, data: dict, max_attempts: int = 4) -> dict:
    """tg_api без файлов с автоматическим retry при flood-wait (429)."""
    res = {}
    for attempt in range(max_attempts):
        res = await tg_api(method, data=data)
        if not res.get("ok"):
            desc = res.get("description", "")
            if "retry after" in desc.lower() or res.get("error_code") == 429:
                retry_after = res.get("parameters", {}).get("retry_after", 5)
                log.warning(f"Flood wait {retry_after}s на методе {method} (попытка {attempt + 1})")
                await asyncio.sleep(retry_after + 1)
                continue
        return res
    return res


async def get_custom_emoji_info(client: Client, custom_emoji_id: str) -> dict | None:
    try:
        stickers = await client.get_custom_emoji_stickers([custom_emoji_id])
        if stickers:
            s = stickers[0]
            fmt = "video" if s.is_video else "animated" if s.is_animated else "static"
            return {"file_id": s.file_id, "format": fmt}
    except Exception:
        pass

    res = await tg_api("getCustomEmojiStickers", data={"custom_emoji_ids": json.dumps([custom_emoji_id])})
    if res.get("ok") and res.get("result"):
        s = res["result"][0]
        fmt = "video" if s.get("is_video") else "animated" if s.get("is_animated") else "static"
        return {"file_id": s["file_id"], "format": fmt}

    return None


async def check_subscriptions(client: Client, uid: int) -> tuple[bool, list]:
    channels = db.db_get_channels()
    if not channels:
        return True, []
    missing = []
    for _, chat_id, chat_url, chat_title in channels:
        try:
            member = await client.get_chat_member(chat_id, uid)
            if member.status in (
                enums.ChatMemberStatus.LEFT,
                enums.ChatMemberStatus.BANNED,
                enums.ChatMemberStatus.RESTRICTED,
            ):
                missing.append((chat_title, chat_url))
        except Exception:
            missing.append((chat_title, chat_url))
    return len(missing) == 0, missing
