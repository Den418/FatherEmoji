import asyncio
import json
import logging
import os
import shutil
import time
import uuid

from hydrogram import enums
from hydrogram.types import Message

import db
import jobs
import keyboards
import media
import recolor
import re
import states
import textemoji
import texts as T
import vector
from tg import (JobCancelled, bot_suffix, download_file, get_custom_emoji_info, h, pack_link,
                tg_api, tg_api_retry)

log = logging.getLogger("fatheremoji")


def progress_bar(done: int, total: int, width: int = 12) -> str:
    filled = int(width * done / total) if total else 0
    return "▓" * filled + "░" * (width - filled)


def friendly_error(desc: str) -> str:
    """Ошибка Bot API простыми словами."""
    d = (desc or "").lower()
    if "too much" in d or "stickers_too_much" in d:
        return T.ERR_PACK_FULL
    if "retry after" in d or "too many requests" in d:
        return T.ERR_FLOOD
    if "too big" in d or "_big" in d:
        return T.ERR_TOO_BIG
    if "dimensions" in d:
        return T.ERR_DIMENSIONS
    if "format" in d or "invalid_sticker" in d or "wrong file" in d:
        return T.ERR_FORMAT
    return h(desc) if desc else T.ERR_UNKNOWN


def _is_full(desc: str) -> bool:
    d = (desc or "").lower()
    return "too much" in d or "stickers_too_much" in d


async def add_to_pack(uid: int, link: str, title: str, sticker_obj: dict,
                      path: str | None = None, name: str = "", mime: str = "") -> dict:
    """
    addStickerToSet; если пака ещё нет, создаёт его (createNewStickerSet) с этим эмодзи.
    path: файл для загрузки (sticker_obj["sticker"] тогда "attach://f"), иначе file_id.
    """
    spec = ("f", path, name, mime) if path else None
    res = await tg_api_retry(
        "addStickerToSet",
        {"user_id": uid, "name": link, "sticker": json.dumps(sticker_obj)},
        max_attempts=8,
        file_spec=spec,
    )
    if not res.get("ok") and "STICKERSET_INVALID" in res.get("description", ""):
        res = await tg_api_retry(
            "createNewStickerSet",
            {"user_id": uid, "name": link, "title": title[:64], "sticker_type": "custom_emoji",
             "stickers": json.dumps([sticker_obj])},
            max_attempts=8,
            file_spec=spec,
        )
        if res.get("ok"):
            db.db_add_pack(uid, link, title)
    return res


async def dest_format(link: str) -> str | None:
    """Формат уже существующего пака (static/animated/video) или None, если пак пуст или его нет."""
    res = await tg_api("getStickerSet", data={"name": link})
    if not res.get("ok"):
        return None
    stickers = res["result"].get("stickers", [])
    if not stickers:
        return None
    s = stickers[0]
    return "video" if s.get("is_video") else "animated" if s.get("is_animated") else "static"


async def transplant(uid: int, link: str, title: str, file_id: str, src_fmt: str,
                     emoji: str, st: dict) -> tuple[bool, str, str]:
    """
    Кладёт чужой стикер или эмодзи в пак. Сначала самый быстрый путь (по file_id),
    а если Telegram отказал (тяжёлый файл, размер 512 вместо 100, чужой формат),
    файл скачивается, сжимается или конвертируется под формат пака и грузится заново.

    st["target"]: формат пака. Обновляется после первого успеха.
    Возвращает (успех, пометка, ошибка). Пометка: "" | "converted" | "frame".
    """
    target = st.get("target")
    if target in (None, src_fmt):
        obj = {"sticker": file_id, "format": src_fmt, "emoji_list": [emoji]}
        res = await add_to_pack(uid, link, title, obj)
        if res.get("ok"):
            st["target"] = target or src_fmt
            return True, "", ""
        desc = res.get("description", "")
        if _is_full(desc):
            return False, "", friendly_error(desc)

    tmp = f"temp/{uid}_{uuid.uuid4().hex[:8]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        raw = os.path.join(tmp, "raw")
        if not await download_file(file_id, raw):
            return False, "", T.ERR_DOWNLOAD
        kind = await asyncio.to_thread(media.detect_type, raw)
        if kind == "empty":
            return False, "", T.ERR_DOWNLOAD
        try:
            out, fmt, mime, name, note = await media.adapt(raw, kind, target, tmp)
        except media.AdaptError as e:
            return False, "", T.ADAPT_ERRORS.get(e.reason, T.ERR_UNKNOWN)
        except Exception as e:
            log.error(f"adapt error: {e}", exc_info=True)
            return False, "", T.ERR_UNKNOWN

        obj = {"sticker": "attach://f", "format": fmt, "emoji_list": [emoji]}
        res = await add_to_pack(uid, link, title, obj, path=out, name=name, mime=mime)
        if res.get("ok"):
            st["target"] = target or fmt
            return True, (note or "converted"), ""
        return False, "", friendly_error(res.get("description", ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _last_custom_emoji_id(link: str) -> str | None:
    set_res = await tg_api("getStickerSet", data={"name": link})
    if not set_res.get("ok"):
        return None
    stickers = set_res["result"].get("stickers", [])
    return stickers[-1].get("custom_emoji_id") if stickers else None


# ════════════════════════════════════════════════════════════════
#  ДОБАВЛЕНИЕ МЕДИА (файл с диска: картинка / видео / tgs)
# ════════════════════════════════════════════════════════════════

async def process_media(client, message: Message, uid: int, force_vector: bool | None = None):
    """
    force_vector=None - берём режим векторизации из состояния пользователя
    ("off"/"on"/"ask"); в режиме "ask" картинка сначала предъявляется
    пользователю с кнопками «вектором / обычной».
    force_vector=True/False - явное решение (после ответа на вопрос).
    """
    data = states.user_states.get(uid, {})
    link = data.get("link")
    if not link:
        return await message.reply(T.NO_ACTIVE_PACK, parse_mode=enums.ParseMode.HTML)

    vector_mode = data.get("vector_mode", "off")
    vector = force_vector if force_vector is not None else vector_mode == "on"
    ask_now = force_vector is None and vector_mode == "ask"

    emoji = data.pop("next_emoji", None) or data.get("default_emoji", "✨")

    msg = await message.reply(T.VECT_PROCESSING if vector else T.PROCESSING)
    tmp = f"temp/{uid}_{message.id}"
    os.makedirs(tmp, exist_ok=True)

    try:
        raw_path = await message.download(f"{tmp}/raw")
        file_type = await asyncio.to_thread(media.detect_type, raw_path)

        if file_type == "empty":
            return await msg.edit_text(T.EMPTY_FILE)

        # Режим «спрашивать»: картинку откладываем и уточняем у пользователя
        if ask_now and file_type == "image":
            data["next_emoji"] = emoji   # вернём реакцию на место - возьмём после ответа
            data["ask_msg"] = message
            await msg.edit_text(
                T.ASK_IMAGE,
                reply_markup=keyboards.kb_vec_ask(),
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if file_type == "tgs":
            out_path = f"{tmp}/ready.tgs"
            if os.path.getsize(raw_path) > media.TGS_LIMIT:
                try:
                    await media.shrink_tgs(raw_path, out_path)
                except media.ImageTooComplex:
                    return await msg.edit_text(T.TGS_TOO_HEAVY, parse_mode=enums.ParseMode.HTML)
            else:
                os.rename(raw_path, out_path)
            fmt = "animated"
            mime_type = "application/x-tgsticker"
            upload_name = "ready.tgs"

        elif file_type in ("webm", "video"):
            # В векторном режиме видео не принимается вовсе: в один пак нельзя
            # смешивать форматы, а вектор - это animated.
            if vector_mode == "on":
                return await msg.edit_text(T.VECT_ONLY_IMAGES)

            out_path = f"{tmp}/ready.webm"
            # mp4/gif/avi физически не несут альфа-канал; настоящий webm может быть
            # прозрачным, поэтому для него до последнего пытаемся альфу сохранить.
            ok = await media.compress_video(raw_path, out_path, no_alpha=(file_type == "video"))
            if not ok:
                return await msg.edit_text(T.MEDIA_TOO_HEAVY)
            fmt = "video"
            mime_type = "video/webm"
            upload_name = "ready.webm"

        else:
            if vector:
                # Векторный режим: растр → SVG → Lottie → .tgs 512×512.
                out_path = f"{tmp}/ready.tgs"
                try:
                    await media.vectorize_image(raw_path, out_path)
                except media.ImageTooComplex:
                    return await msg.edit_text(T.VECT_TOO_COMPLEX)
                except media.VectorizerNotInstalled as e:
                    log.error(f"Модуль векторизации не установлен: {e}")
                    return await msg.edit_text(T.VECT_NOT_INSTALLED)
                fmt = "animated"
                mime_type = "application/x-tgsticker"
                upload_name = "ready.tgs"
            else:
                out_path = f"{tmp}/ready.webp"
                ok = await media.resize_image(raw_path, out_path)
                if not ok:
                    return await msg.edit_text(T.BAD_IMAGE)
                fmt = "static"
                mime_type = "image/webp"
                upload_name = "ready.webp"

        sticker_obj = {"sticker": "attach://f", "format": fmt, "emoji_list": [emoji]}
        res = await add_to_pack(uid, link, data.get("title", "My Emoji Pack"), sticker_obj,
                                path=out_path, name=upload_name, mime=mime_type)

        if not res.get("ok"):
            desc = res.get("description", "")
            if "format" in desc.lower() or "invalid_sticker" in desc.lower():
                return await msg.edit_text(T.FORMAT_CONFLICT, parse_mode=enums.ParseMode.HTML)

        if res.get("ok"):
            custom_emoji_id = await _last_custom_emoji_id(link)
            emoji_display = f'<emoji id="{custom_emoji_id}">{emoji}</emoji>' if custom_emoji_id else emoji
            await msg.edit_text(
                T.ADDED.format(emoji=emoji_display, link=pack_link(link)),
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            desc = res.get("description", "")
            if "file is too big" in desc.lower():
                friendly = T.TG_FILE_TOO_BIG
            elif "wrong file type" in desc.lower() or "invalid" in desc.lower():
                friendly = T.TG_BAD_FORMAT
            else:
                friendly = T.TG_ERROR.format(desc=friendly_error(desc))
            await msg.edit_text(friendly, parse_mode=enums.ParseMode.HTML)

    except Exception as e:
        log.error(f"process_media exception: {e}", exc_info=True)
        await msg.edit_text(T.INTERNAL_ERROR.format(desc=h(e)), parse_mode=enums.ParseMode.HTML)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
#  КОПИРОВАНИЕ ЧУЖИХ ПРЕМИУМ-ЭМОДЗИ ИЗ ЧАТА
# ════════════════════════════════════════════════════════════════

async def add_custom_emojis(client, message: Message, uid: int, ce_entities: list):
    data = states.user_states.get(uid, {})
    link = data.get("link")
    if not link:
        return await message.reply(T.NO_ACTIVE_SHORT, parse_mode=enums.ParseMode.HTML)
    if jobs.busy(uid):
        return await message.reply(T.JOB_BUSY)

    default_emoji = data.get("default_emoji", "✨")
    title = data.get("title", "My Emoji Pack")
    st = {"target": await dest_format(link)}
    converted = 0
    errors = []
    total = len(ce_entities)

    msg = await message.reply(T.COPYING_EMOJIS.format(n=total))
    job = jobs.Job(uid, msg, total, head=T.COPYING_EMOJIS.format(n=total))
    stopped = False

    async with jobs.running(job):
        for idx, ent in enumerate(ce_entities):
            if job.cancel:
                stopped = True
                break
            t0, f0 = time.monotonic(), job.flood_time
            ok = False
            try:
                info = await get_custom_emoji_info(client, ent.custom_emoji_id)
                if not info:
                    errors.append(T.HIDDEN_EMOJI.format(n=idx + 1))
                else:
                    ok, note, err = await transplant(uid, link, title, info["file_id"], info["format"],
                                                     default_emoji, st)
                    if ok:
                        converted += 1 if note else 0
                    else:
                        errors.append(T.COPY_ONE_ERR.format(n=idx + 1, desc=err))
            except JobCancelled:
                stopped = True
                break
            job.record((time.monotonic() - t0) - (job.flood_time - f0), ok)
            await job.update()

    lines = [(T.CLONE_STOPPED if stopped else T.COPY_RESULT).format(added=job.added, total=total)]
    if converted:
        lines.append(T.COPY_CONVERTED.format(n=converted))
    if errors:
        lines.append("\n<blockquote expandable>" + T.COPY_ERRORS + "\n" + "\n".join(errors) + "</blockquote>")
    lines.append(f"\n{pack_link(link)}")
    await job.finish("\n".join(lines))


# ════════════════════════════════════════════════════════════════
#  КОПИРОВАНИЕ ЧУЖОГО ПАКА ЦЕЛИКОМ ПО ССЫЛКЕ
# ════════════════════════════════════════════════════════════════

async def copy_entire_pack(client, message: Message, uid: int, source_pack: str):
    data = states.user_states.get(uid, {})
    link = data.get("link")
    if not link:
        return await message.reply(T.NO_ACTIVE_PACK, parse_mode=enums.ParseMode.HTML)
    if jobs.busy(uid):
        return await message.reply(T.JOB_BUSY)

    res = await tg_api("getStickerSet", data={"name": source_pack})
    if not res.get("ok"):
        return await message.reply(T.CLONE_NOT_FOUND, parse_mode=enums.ParseMode.HTML)

    stickers = res.get("result", {}).get("stickers", [])
    if not stickers:
        return await message.reply(T.CLONE_EMPTY, parse_mode=enums.ParseMode.HTML)

    title = data.get("title", "My Emoji Pack")
    st = {"target": await dest_format(link)}
    total = len(stickers)
    msg = await message.reply(f"{T.CLONING}\n\n⏳ 0 / {total}", parse_mode=enums.ParseMode.HTML)
    job = jobs.Job(uid, msg, total, head=T.CLONING)
    converted = 0
    errors = []
    stopped = False

    async with jobs.running(job):
        for idx, s in enumerate(stickers):
            if job.cancel:
                stopped = True
                break
            fmt = "video" if s.get("is_video") else "animated" if s.get("is_animated") else "static"
            t0, f0 = time.monotonic(), job.flood_time
            try:
                ok, note, err = await transplant(uid, link, title, s["file_id"], fmt, s.get("emoji", "✨"), st)
            except JobCancelled:
                stopped = True
                break
            if ok:
                converted += 1 if note else 0
            else:
                errors.append(f"#{idx + 1}: {err}")
            job.record((time.monotonic() - t0) - (job.flood_time - f0), ok)
            if not ok and err == T.ERR_PACK_FULL:
                break
            await job.update()

    lines = [(T.CLONE_STOPPED if stopped else T.CLONE_RESULT).format(added=job.added, total=total)]
    if converted:
        lines.append(T.COPY_CONVERTED.format(n=converted))
    if errors:
        shown = errors[:6]
        tail = ("\n" + T.CLONE_MORE.format(n=len(errors) - 6)) if len(errors) > 6 else ""
        lines.append("\n<blockquote expandable>" + T.CLONE_ERRORS + "\n" + "\n".join(shown) + tail + "</blockquote>")
    lines.append(f"\n{pack_link(link)}")
    await job.finish("\n".join(lines))


# ════════════════════════════════════════════════════════════════
#  ПЕРЕДАЧА ПАКА
# ════════════════════════════════════════════════════════════════

async def transfer_pack(client, message: Message, uid: int, recipient_id: int, source_pack: str):
    """
    «Передача» пака: Telegram не даёт сменить owner'а существующего сета, поэтому
    под капотом это клон - новый custom_emoji-сет создаётся на recipient_id, все
    эмодзи копируются в него, и только после проверки, что скопировалось всё до
    единого, оригинал удаляется.
    """
    res = await tg_api("getStickerSet", data={"name": source_pack})
    if not res.get("ok"):
        return await message.reply(T.TRANSFER_SRC_GONE, parse_mode=enums.ParseMode.HTML)

    result = res["result"]
    stickers = result.get("stickers", [])
    title = result.get("title", source_pack)

    if not stickers:
        return await message.reply(T.TRANSFER_EMPTY, parse_mode=enums.ParseMode.HTML)

    total = len(stickers)
    suffix = bot_suffix()
    new_link = f"tr{recipient_id}{int(time.time())}"[: 64 - len(suffix)] + suffix

    msg = await message.reply(f"{T.TRANSFERRING}\n\n⏳ 0 / {total}", parse_mode=enums.ParseMode.HTML)
    added = 0
    errors = []

    for idx, s in enumerate(stickers):
        fmt = "video" if s.get("is_video") else "animated" if s.get("is_animated") else "static"
        sticker_obj = {"sticker": s["file_id"], "format": fmt, "emoji_list": [s.get("emoji", "✨")]}

        if idx == 0:
            add_res = await tg_api_retry(
                "createNewStickerSet",
                data={
                    "user_id": recipient_id,
                    "name": new_link,
                    "title": title,
                    "sticker_type": "custom_emoji",
                    "stickers": json.dumps([sticker_obj]),
                },
            )
        else:
            add_res = await tg_api_retry(
                "addStickerToSet",
                data={"user_id": recipient_id, "name": new_link, "sticker": json.dumps(sticker_obj)},
            )

        if add_res.get("ok"):
            added += 1
        else:
            desc = add_res.get("description", "")
            if idx == 0:
                # Не смогли создать пак получателю вообще - дальше пытаться бессмысленно,
                # исходный пак в этом случае не трогаем.
                await msg.edit_text(
                    T.TRANSFER_FAIL_CREATE.format(desc=h(desc)),
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            errors.append(f"#{idx + 1}: {desc}")

        if (idx + 1) % 5 == 0 or (idx + 1) == total:
            bar = progress_bar(idx + 1, total)
            try:
                await msg.edit_text(f"{T.TRANSFERRING}\n\n[{bar}] {idx + 1} / {total}")
            except Exception:
                pass

    # Независимая проверка перед удалением оригинала - не полагаемся только на
    # счётчик из цикла: мог быть промежуточный сбой на отдельном элементе.
    check = await tg_api("getStickerSet", data={"name": new_link})
    new_count = len(check.get("result", {}).get("stickers", [])) if check.get("ok") else 0

    if new_count == 0:
        return await msg.edit_text(T.TRANSFER_NONE, parse_mode=enums.ParseMode.HTML)

    if new_count < total:
        lines = [T.TRANSFER_PARTIAL_HEAD.format(done=new_count, total=total)]
        if errors:
            shown = errors[:5]
            tail = ("\n" + T.CLONE_MORE.format(n=len(errors) - 5)) if len(errors) > 5 else ""
            lines.append("\n<blockquote expandable>" + "\n".join(shown) + tail + "</blockquote>")
        lines.append(T.TRANSFER_PARTIAL)
        return await msg.edit_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)

    # Всё скопировалось - теперь можно удалять оригинал
    del_res = await tg_api_retry("deleteStickerSet", data={"name": source_pack})
    db.db_remove_pack(uid, source_pack)
    db.db_add_pack(recipient_id, new_link, title)

    if del_res.get("ok"):
        await msg.edit_text(
            T.TRANSFER_DONE.format(count=new_count, uid=recipient_id),
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await msg.edit_text(
            T.TRANSFER_DEL_FAIL.format(count=new_count, desc=h(del_res.get("description", ""))),
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        await client.send_message(
            recipient_id,
            T.TRANSFER_RECV.format(title=h(title), count=new_count, link=pack_link(new_link)),
            disable_web_page_preview=True,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
#  ОБЛОЖКА ПАКА
# ════════════════════════════════════════════════════════════════

async def set_pack_thumb_from_emoji(client, message: Message, uid: int, custom_emoji_id: str):
    """Ставит обложкой один из уже существующих эмодзи пака."""
    link = states.user_states.get(uid, {}).get("edit_link")
    if not link:
        return await message.reply(T.THUMB_PACK_GONE, parse_mode=enums.ParseMode.HTML)

    msg = await message.reply(T.THUMB_SETTING)
    res = await tg_api(
        "setCustomEmojiStickerSetThumbnail",
        data={"name": link, "custom_emoji_id": custom_emoji_id},
    )
    states.user_states[uid] = {}
    if res.get("ok"):
        await msg.edit_text(T.THUMB_DONE, parse_mode=enums.ParseMode.HTML)
        await message.reply(T.MENU_NEXT, reply_markup=keyboards.MAIN_KB)
    else:
        await msg.edit_text(
            T.TG_ERROR.format(desc=h(res.get("description"))) + "\n\n" + T.THUMB_ERR_CHECK,
            parse_mode=enums.ParseMode.HTML,
        )


async def set_pack_thumb(client, message: Message, uid: int):
    """
    Обложкой custom-emoji пака Telegram разрешает ставить только один из эмодзи
    самого пака (метод не принимает произвольный файл). Поэтому присланная
    картинка добавляется в пак новым эмодзи и тут же назначается обложкой.
    """
    link = states.user_states.get(uid, {}).get("edit_link")
    if not link:
        return await message.reply(T.THUMB_PACK_GONE, parse_mode=enums.ParseMode.HTML)

    msg = await message.reply(T.THUMB_ADDING)
    tmp = f"temp/{uid}_thumb"
    os.makedirs(tmp, exist_ok=True)
    try:
        raw = await message.download(f"{tmp}/thumb_raw")
        thumb = f"{tmp}/thumb.webp"
        ok = await media.resize_image(raw, thumb)
        if not ok:
            states.user_states[uid] = {}
            return await msg.edit_text(T.THUMB_BAD_FILE)

        sticker_obj = {"sticker": "attach://f", "format": "static", "emoji_list": ["✨"]}
        res = await tg_api(
            "addStickerToSet",
            data={"user_id": uid, "name": link, "sticker": json.dumps(sticker_obj)},
            files={"f": ("thumb.webp", open(thumb, "rb"), "image/webp")},
        )

        if not res.get("ok"):
            desc = res.get("description", "")
            states.user_states[uid] = {}
            if "format" in desc.lower() or "invalid_sticker" in desc.lower():
                return await msg.edit_text(T.THUMB_CONFLICT_HINT, parse_mode=enums.ParseMode.HTML)
            return await msg.edit_text(T.TG_ERROR.format(desc=h(desc)), parse_mode=enums.ParseMode.HTML)

        custom_emoji_id = await _last_custom_emoji_id(link)
        if not custom_emoji_id:
            states.user_states[uid] = {}
            return await msg.edit_text(T.THUMB_NO_ID)

        thumb_res = await tg_api(
            "setCustomEmojiStickerSetThumbnail",
            data={"name": link, "custom_emoji_id": custom_emoji_id},
        )
        states.user_states[uid] = {}
        if thumb_res.get("ok"):
            await msg.edit_text(T.THUMB_ADD_DONE, parse_mode=enums.ParseMode.HTML)
            await message.reply(T.MENU_NEXT, reply_markup=keyboards.MAIN_KB)
        else:
            await msg.edit_text(
                T.THUMB_SET_FAIL.format(desc=h(thumb_res.get("description"))),
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        states.user_states[uid] = {}
        await msg.edit_text(T.INTERNAL_ERROR.format(desc=h(e)), parse_mode=enums.ParseMode.HTML)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
#  ТЕКСТОВЫЕ ЭМОДЗИ
# ════════════════════════════════════════════════════════════════

_TEXT_UPLOAD = {
    "static":   ("image/webp", "text.webp"),
    "animated": ("application/x-tgsticker", "text.tgs"),
    "video":    ("video/webm", "text.webm"),
}


async def add_text_emoji(uid: int, link: str, title: str, style, emoji: str) -> dict:
    """
    Рисует текстовый эмодзи и кладёт его в пак.

    Формат пака задаётся первым эмодзи и дальше не меняется. Если пак уже существует,
    эмодзи строится сразу в его формате (со всеми эффектами и анимацией, которые этот
    формат умеет), а не конвертируется из другого.

    -> {"ok": bool, "err": str, "cid": id кастомного эмодзи, "fmt": static|vector|video, "notes": [...]}
    """
    target = await dest_format(link)
    tmp = f"temp/{uid}_txt_{uuid.uuid4().hex[:6]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        s = style.copy()
        if target:
            s.fmt = textemoji.TG_TO_FMT[target]
        path, tg_fmt, notes = await asyncio.to_thread(textemoji.build, s, tmp)
        mime, name = _TEXT_UPLOAD[tg_fmt]
        obj = {"sticker": "attach://f", "format": tg_fmt, "emoji_list": [emoji]}
        res = await add_to_pack(uid, link, title, obj, path=path, name=name, mime=mime)
        if not res.get("ok"):
            return {"ok": False, "err": friendly_error(res.get("description", "")), "cid": None, "fmt": s.fmt, "notes": []}
        return {"ok": True, "err": "", "cid": await _last_custom_emoji_id(link), "fmt": s.fmt, "notes": notes}
    except vector.ImageTooComplex:
        return {"ok": False, "err": T.TXT_TOO_COMPLEX, "cid": None, "fmt": style.fmt, "notes": []}
    except RuntimeError:
        return {"ok": False, "err": T.ADAPT_ERRORS["too_heavy"], "cid": None, "fmt": style.fmt, "notes": []}
    except Exception as e:
        log.error(f"add_text_emoji: {e}", exc_info=True)
        return {"ok": False, "err": T.ERR_UNKNOWN, "cid": None, "fmt": style.fmt, "notes": []}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
#  ДОСТАТЬ ИСХОДНИК ЭМОДЗИ
# ════════════════════════════════════════════════════════════════

async def send_emoji_files(client, message: Message, entities: list):
    sent = 0
    for ent in entities[:10]:
        info = await get_custom_emoji_info(client, ent.custom_emoji_id)
        if not info:
            await message.reply(T.NO_EMOJI_INFO)
            continue
        tmp = f"temp/{message.from_user.id}_{uuid.uuid4().hex[:6]}"
        os.makedirs(tmp, exist_ok=True)
        try:
            ext = {"static": "webp", "animated": "tgs", "video": "webm"}[info["format"]]
            path = os.path.join(tmp, f"emoji_{str(ent.custom_emoji_id)[-6:]}.{ext}")
            if not await download_file(info["file_id"], path):
                await message.reply(T.ERR_DOWNLOAD)
                continue
            await message.reply_document(path, caption=T.FILE_CAPTION.format(fmt=T.FMT_NAMES[info["format"]]),
                                         parse_mode=enums.ParseMode.HTML)
            sent += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return sent


# ════════════════════════════════════════════════════════════════
#  ПЕРЕКРАСКА ПАКА
# ════════════════════════════════════════════════════════════════

async def resolve_pack_name(client, message: Message) -> tuple[str | None, str | None]:
    """Имя пака из ссылки, из текста или из присланного эмодзи/стикера. -> (имя, откуда)"""
    ents = [e for e in (message.entities or []) if e.type == enums.MessageEntityType.CUSTOM_EMOJI]
    if ents:
        info = await get_custom_emoji_info(client, ents[0].custom_emoji_id)
        if info and info.get("set_name"):
            return info["set_name"], "emoji"
        return None, "hidden"
    if message.sticker and message.sticker.set_name:
        return message.sticker.set_name, "sticker"
    text = (message.text or "").strip()
    m = re.search(r"(?:addemoji|addstickers)/([A-Za-z0-9_]+)", text)
    if m:
        return m.group(1), "link"
    if re.fullmatch(r"[A-Za-z0-9_]{3,64}", text):
        return text, "name"
    return None, None


async def _recolor_one(uid: int, s: dict, idx: int, new_link: str, new_title: str,
                       hex_color: str, mode: str, st: dict) -> tuple[bool, str, bool]:
    """Перекрашивает один эмодзи и кладёт его в новый пак. -> (успех, текст ошибки, остановить всё)."""
    tmp = f"temp/{uid}_rc_{uuid.uuid4().hex[:6]}"
    os.makedirs(tmp, exist_ok=True)
    try:
        raw = os.path.join(tmp, "raw")
        if not await download_file(s["file_id"], raw):
            return False, f"#{idx + 1}: {T.ERR_DOWNLOAD}", False
        kind = await asyncio.to_thread(media.detect_type, raw)
        try:
            if kind == "tgs":
                out, fmt, mime, name = os.path.join(tmp, "o.tgs"), "animated", "application/x-tgsticker", "o.tgs"
                await asyncio.to_thread(recolor.recolor_tgs_fit, raw, out, hex_color, mode)
            elif kind in ("webm", "video"):
                out, fmt, mime, name = os.path.join(tmp, "o.webm"), "video", "video/webm", "o.webm"
                if not await asyncio.to_thread(recolor.recolor_webm, raw, out, hex_color, mode):
                    raise media.AdaptError("too_heavy")
            elif kind == "image":
                out, fmt, mime, name = os.path.join(tmp, "o.webp"), "static", "image/webp", "o.webp"
                await asyncio.to_thread(recolor.recolor_image, raw, out, hex_color, mode)
            else:
                return False, f"#{idx + 1}: {T.ERR_DOWNLOAD}", False
        except vector.ImageTooComplex:
            return False, f"#{idx + 1}: {T.ADAPT_ERRORS['too_complex']}", False
        except media.AdaptError as e:
            return False, f"#{idx + 1}: {T.ADAPT_ERRORS.get(e.reason, T.ERR_UNKNOWN)}", False
        except Exception as e:
            log.error(f"recolor #{idx}: {e}", exc_info=True)
            return False, f"#{idx + 1}: {T.ERR_UNKNOWN}", False

        if st["target"] and st["target"] != fmt:
            return False, f"#{idx + 1}: {T.ERR_FORMAT}", False

        obj = {"sticker": "attach://f", "format": fmt, "emoji_list": [s.get("emoji", "✨")]}
        r = await add_to_pack(uid, new_link, new_title, obj, path=out, name=name, mime=mime)
        if r.get("ok"):
            st["target"] = fmt
            return True, "", False
        desc = r.get("description", "")
        return False, f"#{idx + 1}: {friendly_error(desc)}", _is_full(desc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def recolor_pack(client, job: "jobs.Job", src_pack: str, hex_color: str, mode: str):
    """
    Копия пака в новом цвете. Работает как долгая задача: показывает прогресс и оставшееся время,
    после каждых BATCH_SIZE добавленных эмодзи делает паузу, отсчитывает ожидания Telegram и
    останавливается по кнопке (то, что успело перекраситься, остаётся в новом паке).
    """
    uid = job.uid
    res = await tg_api("getStickerSet", data={"name": src_pack})
    if not res.get("ok"):
        return await job.finish(T.CLONE_NOT_FOUND)
    stickers = res["result"].get("stickers", [])[:200]
    title = res["result"].get("title", src_pack)
    if not stickers:
        return await job.finish(T.CLONE_EMPTY)

    total = len(stickers)
    job.total, job.pacing = total, True
    job.head = T.RC_HEAD
    job.extra = T.RC_EXTRA.format(color=hex_color)
    new_link = f"rc{uuid.uuid4().hex[:10]}{bot_suffix()}"
    new_title = f"{title[:50]} {hex_color}"[:64]
    st = {"target": None}
    errors = []
    stopped = False

    async with jobs.running(job):
        await job.update(every=0)
        for idx, s in enumerate(stickers):
            if job.cancel:
                stopped = True
                break
            t0, f0 = time.monotonic(), job.flood_time
            try:
                ok, err, fatal = await _recolor_one(uid, s, idx, new_link, new_title, hex_color, mode, st)
            except JobCancelled:
                stopped = True
                break
            job.record((time.monotonic() - t0) - (job.flood_time - f0), ok)
            if err:
                errors.append(err)
            if fatal:
                break
            await job.update()
            if job.needs_pause():
                try:
                    await job.pause()
                except JobCancelled:
                    stopped = True
                    break

    if job.added == 0:
        return await job.finish(T.RC_NONE)

    head = (T.RC_STOPPED if stopped else T.RC_DONE).format(added=job.added, total=total, color=hex_color)
    lines = [head]
    if errors:
        shown = errors[:6]
        tail = ("\n" + T.CLONE_MORE.format(n=len(errors) - 6)) if len(errors) > 6 else ""
        lines.append("\n<blockquote expandable>" + T.CLONE_ERRORS + "\n" + "\n".join(shown) + tail + "</blockquote>")
    lines.append(f"\n{pack_link(new_link)}")
    await job.finish("\n".join(lines))
