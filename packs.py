import asyncio
import json
import logging
import os
import shutil
import time

from hydrogram import enums
from hydrogram.types import Message

import db
import keyboards
import media
import states
import texts as T
from tg import bot_suffix, get_custom_emoji_info, h, pack_link, tg_api, tg_api_retry

log = logging.getLogger("fatheremoji")


def progress_bar(done: int, total: int, width: int = 12) -> str:
    filled = int(width * done / total) if total else 0
    return "▓" * filled + "░" * (width - filled)


async def _upload_sticker(uid: int, link: str, sticker_obj: dict, out_path: str,
                          upload_name: str, mime_type: str, pack_title: str) -> dict:
    """addStickerToSet с автосозданием пака, если его ещё нет у Telegram."""
    files = {"f": (upload_name, open(out_path, "rb"), mime_type)}
    res = await tg_api(
        "addStickerToSet",
        data={"user_id": uid, "name": link, "sticker": json.dumps(sticker_obj)},
        files=files,
    )
    if not res.get("ok") and "STICKERSET_INVALID" in res.get("description", ""):
        res = await tg_api(
            "createNewStickerSet",
            data={
                "user_id": uid,
                "name": link,
                "title": pack_title,
                "sticker_type": "custom_emoji",
                "stickers": json.dumps([sticker_obj]),
            },
            files={"f": (upload_name, open(out_path, "rb"), mime_type)},
        )
        if res.get("ok"):
            db.db_add_pack(uid, link, pack_title)
    return res


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
    force_vector=None — берём режим векторизации из состояния пользователя
    ("off"/"on"/"ask"); в режиме "ask" картинка сначала предъявляется
    пользователю с кнопками «вектором / обычной».
    force_vector=True/False — явное решение (после ответа на вопрос).
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
            data["next_emoji"] = emoji   # вернём реакцию на место — возьмём после ответа
            data["ask_msg"] = message
            await msg.edit_text(
                T.ASK_IMAGE,
                reply_markup=keyboards.kb_vec_ask(),
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if file_type == "tgs":
            out_path = f"{tmp}/ready.tgs"
            os.rename(raw_path, out_path)
            fmt = "animated"
            mime_type = "application/x-tgsticker"
            upload_name = "ready.tgs"

        elif file_type in ("webm", "video"):
            # В векторном режиме видео не принимается вовсе: в один пак нельзя
            # смешивать форматы, а вектор — это animated.
            if vector_mode == "on":
                return await msg.edit_text(T.VECT_ONLY_IMAGES)

            out_path = f"{tmp}/ready.webm"
            # mp4/gif/avi физически не несут альфа-канал — сразу кодируем без него
            # (легче для битрейта); настоящий webm может быть прозрачным, поэтому
            # для него до последнего пытаемся альфу сохранить.
            no_alpha = (file_type == "video")

            ok = await media.convert_to_webm(raw_path, out_path, no_alpha=no_alpha)
            if not ok:
                ok = await media.convert_to_webm(raw_path, out_path, fallback_bitrate=True, no_alpha=no_alpha)
            if not ok:
                ok = await media.convert_to_webm(raw_path, out_path, fallback_bitrate=True, no_alpha=no_alpha, max_duration=1.5)
            if not ok and not no_alpha:
                # Последний шанс для webm с настоящей прозрачностью: жертвуем альфой,
                # чтобы файл гарантированно доехал, а не отваливался по размеру.
                ok = await media.convert_to_webm(
                    raw_path, out_path, fallback_bitrate=True, no_alpha=no_alpha,
                    max_duration=1.5, force_drop_alpha=True,
                )
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
        res = await _upload_sticker(uid, link, sticker_obj, out_path,
                                    upload_name, mime_type, data.get("title", "My Emoji Pack"))

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
                friendly = T.TG_ERROR.format(desc=h(desc))
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

    default_emoji = data.get("default_emoji", "✨")
    added = 0
    errors = []

    msg = await message.reply(T.COPYING_EMOJIS.format(n=len(ce_entities)))

    for idx, ent in enumerate(ce_entities):
        info = await get_custom_emoji_info(client, ent.custom_emoji_id)
        if not info:
            errors.append(T.HIDDEN_EMOJI.format(n=idx + 1))
            continue

        sticker_obj = {
            "sticker": info["file_id"],
            "format": info["format"],
            "emoji_list": [default_emoji],
        }

        res = await tg_api_retry(
            "addStickerToSet",
            data={"user_id": uid, "name": link, "sticker": json.dumps(sticker_obj)},
        )

        if not res.get("ok"):
            desc = res.get("description", "")
            if "STICKERSET_INVALID" in desc:
                res = await tg_api_retry(
                    "createNewStickerSet",
                    data={
                        "user_id": uid,
                        "name": link,
                        "title": data.get("title", "My Emoji Pack"),
                        "sticker_type": "custom_emoji",
                        "stickers": json.dumps([sticker_obj]),
                    },
                )
                if res.get("ok"):
                    db.db_add_pack(uid, link, data.get("title", "My Emoji Pack"))
            elif "format" in desc.lower() or "invalid_sticker" in desc.lower():
                errors.append(T.COPY_FMT_ERR.format(n=idx + 1))
            else:
                errors.append(T.COPY_ONE_ERR.format(n=idx + 1, desc=h(desc)))

        if res.get("ok"):
            added += 1

    lines = [T.COPY_RESULT.format(added=added, total=len(ce_entities))]
    if errors:
        lines.append("\n<blockquote expandable>" + T.COPY_ERRORS + "\n" + "\n".join(errors) + "</blockquote>")
    lines.append(f"\n{pack_link(link)}")

    await msg.edit_text(
        "\n".join(lines),
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )


# ════════════════════════════════════════════════════════════════
#  КОПИРОВАНИЕ ЧУЖОГО ПАКА ЦЕЛИКОМ ПО ССЫЛКЕ
# ════════════════════════════════════════════════════════════════

async def copy_entire_pack(client, message: Message, uid: int, source_pack: str):
    data = states.user_states.get(uid, {})
    link = data.get("link")
    if not link:
        return await message.reply(T.NO_ACTIVE_PACK, parse_mode=enums.ParseMode.HTML)

    res = await tg_api("getStickerSet", data={"name": source_pack})
    if not res.get("ok"):
        return await message.reply(T.CLONE_NOT_FOUND, parse_mode=enums.ParseMode.HTML)

    stickers = res.get("result", {}).get("stickers", [])
    if not stickers:
        return await message.reply(T.CLONE_EMPTY, parse_mode=enums.ParseMode.HTML)

    total = len(stickers)
    msg = await message.reply(f"{T.CLONING}\n\n⏳ 0 / {total}", parse_mode=enums.ParseMode.HTML)
    added = 0
    errors = []

    for idx, s in enumerate(stickers):
        fmt = "video" if s.get("is_video") else "animated" if s.get("is_animated") else "static"
        sticker_obj = {"sticker": s["file_id"], "format": fmt, "emoji_list": [s.get("emoji", "✨")]}

        add_res = await tg_api_retry(
            "addStickerToSet",
            data={"user_id": uid, "name": link, "sticker": json.dumps(sticker_obj)},
        )

        if not add_res.get("ok"):
            desc = add_res.get("description", "")
            if "STICKERSET_INVALID" in desc:
                add_res = await tg_api_retry(
                    "createNewStickerSet",
                    data={
                        "user_id": uid,
                        "name": link,
                        "title": data.get("title", "My Emoji Pack"),
                        "sticker_type": "custom_emoji",
                        "stickers": json.dumps([sticker_obj]),
                    },
                )
                if add_res.get("ok"):
                    db.db_add_pack(uid, link, data.get("title", "My Emoji Pack"))

            if not add_res.get("ok"):
                desc = add_res.get("description", "")
                if "format" in desc.lower():
                    errors.append(f"#{idx + 1} — конфликт форматов")
                else:
                    errors.append(f"#{idx + 1} — {desc}")
            else:
                added += 1
        else:
            added += 1

        if (idx + 1) % 5 == 0 or (idx + 1) == total:
            bar = progress_bar(idx + 1, total)
            try:
                await msg.edit_text(f"{T.CLONING}\n\n[{bar}] {idx + 1} / {total}")
            except Exception:
                pass

    lines = [T.CLONE_RESULT.format(added=added, total=total)]
    if errors:
        shown = errors[:5]
        tail = ("\n" + T.CLONE_MORE.format(n=len(errors) - 5)) if len(errors) > 5 else ""
        lines.append("\n<blockquote expandable>" + T.CLONE_ERRORS + "\n" + "\n".join(shown) + tail + "</blockquote>")
    lines.append(f"\n{pack_link(link)}")

    await msg.edit_text(
        "\n".join(lines),
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )


# ════════════════════════════════════════════════════════════════
#  ПЕРЕДАЧА ПАКА
# ════════════════════════════════════════════════════════════════

async def transfer_pack(client, message: Message, uid: int, recipient_id: int, source_pack: str):
    """
    «Передача» пака: Telegram не даёт сменить owner'а существующего сета, поэтому
    под капотом это клон — новый custom_emoji-сет создаётся на recipient_id, все
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
                # Не смогли создать пак получателю вообще — дальше пытаться бессмысленно,
                # исходный пак в этом случае не трогаем.
                await msg.edit_text(
                    T.TRANSFER_FAIL_CREATE.format(desc=h(desc)),
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            errors.append(f"#{idx + 1} — {desc}")

        if (idx + 1) % 5 == 0 or (idx + 1) == total:
            bar = progress_bar(idx + 1, total)
            try:
                await msg.edit_text(f"{T.TRANSFERRING}\n\n[{bar}] {idx + 1} / {total}")
            except Exception:
                pass

    # Независимая проверка перед удалением оригинала — не полагаемся только на
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

    # Всё скопировалось — теперь можно удалять оригинал
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
