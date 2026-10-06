import asyncio
import contextvars
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


class JobCancelled(Exception):
    """Пользователь остановил долгую операцию (кнопка «Остановить»)."""


# Если операция хочет показывать пользователю ожидание при flood wait, она кладёт сюда
# корутину hook(секунды): та сама отсчитывает паузу и обновляет сообщение с прогрессом.
flood_hook: contextvars.ContextVar = contextvars.ContextVar("flood_hook", default=None)


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


async def tg_api_retry(method: str, data: dict, max_attempts: int = 4, file_spec: tuple | None = None) -> dict:
    """
    tg_api с автоматическим retry при flood-wait (429).
    file_spec = (поле, путь, имя_для_загрузки, mime): файл открывается заново на каждой попытке.
    """
    res = {}
    for attempt in range(max_attempts):
        files = None
        if file_spec:
            field, path, name, mime = file_spec
            files = {field: (name, open(path, "rb"), mime)}
        res = await tg_api(method, data=data, files=files)
        if not res.get("ok"):
            desc = res.get("description", "")
            if "retry after" in desc.lower() or res.get("error_code") == 429:
                retry_after = res.get("parameters", {}).get("retry_after", 5)
                log.warning(f"Flood wait {retry_after}s на методе {method} (попытка {attempt + 1})")
                hook = flood_hook.get()
                if hook:
                    await hook(retry_after + 1)
                else:
                    await asyncio.sleep(retry_after + 1)
                continue
        return res
    return res


async def get_custom_emoji_info(client: Client, custom_emoji_id: int | str) -> dict | None:
    cid = int(custom_emoji_id)      # hydrogram отдаёт id числом, Bot API ждёт строку: приводим сами
    try:
        stickers = await client.get_custom_emoji_stickers([cid])
        if stickers:
            s = stickers[0]
            fmt = "video" if s.is_video else "animated" if s.is_animated else "static"
            return {"file_id": s.file_id, "format": fmt, "set_name": getattr(s, "set_name", None),
                    "emoji": getattr(s, "emoji", None)}
    except Exception:
        pass

    res = await tg_api("getCustomEmojiStickers", data={"custom_emoji_ids": json.dumps([str(cid)])})
    if res.get("ok") and res.get("result"):
        s = res["result"][0]
        fmt = "video" if s.get("is_video") else "animated" if s.get("is_animated") else "static"
        return {"file_id": s["file_id"], "format": fmt, "set_name": s.get("set_name"), "emoji": s.get("emoji")}

    return None


async def download_file(file_id: str, dst: str) -> bool:
    """Скачивает файл по file_id через Bot API (формат file_id у hydrogram и Bot API общий)."""
    info = await tg_api("getFile", data={"file_id": file_id})
    path = (info.get("result") or {}).get("file_path")
    if not path:
        return False

    def _get():
        try:
            r = requests.get(f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{path}", timeout=60)
            if r.status_code != 200:
                return False
            with open(dst, "wb") as f:
                f.write(r.content)
            return True
        except Exception as e:
            log.error(f"download_file error: {e}")
            return False

    return await asyncio.to_thread(_get)


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
