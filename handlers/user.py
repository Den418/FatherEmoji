import re

from hydrogram import enums, filters
from hydrogram.types import Message

import config
import db
import keyboards
import packs
import states
import texts as T
import tg
from tg import app, bot_suffix, get_custom_emoji_info, h, tg_api
from .commands import cmd_help

MENU_ACTIONS = {T.BTN_CREATE, T.BTN_EDIT}


@app.on_message(filters.text & filters.private)
async def handle_text(client, message: Message):
    uid = message.from_user.id
    text = message.text.strip()
    state = states.user_states.get(uid, {}).get("state")

    if text.startswith("/"):
        return

    # ── Кнопка завершения сессии ──
    if text == T.BTN_DONE:
        data = states.user_states.get(uid, {})
        link = data.get("link") or data.get("edit_link") or data.get("delete_link")
        states.user_states[uid] = {}
        if link:
            await message.reply(
                T.DONE_CB.format(link=tg.pack_link(link)),
                reply_markup=keyboards.MAIN_KB,
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await message.reply(T.DONE_PLAIN, reply_markup=keyboards.MAIN_KB)
        return

    # ── Кнопки меню ──
    if text in MENU_ACTIONS:
        ok, missing = await tg.check_subscriptions(client, uid)
        if not ok:
            return await message.reply(T.SUB_LOST, reply_markup=keyboards.kb_sub_check(missing))

    if text == T.BTN_CREATE:
        states.user_states[uid] = {"state": "CREATE_LINK"}
        return await message.reply(T.CREATE_LINK, parse_mode=enums.ParseMode.HTML)

    if text == T.BTN_EDIT:
        return await message.reply(T.ASK_PACK_EDIT, reply_markup=keyboards.kb_user_packs(uid), parse_mode=enums.ParseMode.HTML)

    if text == T.BTN_CHANNEL:
        return await message.reply(T.CHANNEL.format(url=config.CHANNEL_URL))

    if text == T.BTN_HELP:
        return await cmd_help(client, message)

    # ── Админские состояния ──
    if state == "ADD_ADMIN" and db.is_admin(uid):
        try:
            new_id = int(text)
        except ValueError:
            return await message.reply(T.ERR_ID_DIGITS)
        db.db_add_admin(new_id)
        states.user_states[uid] = {}
        return await message.reply(
            T.ADMIN_ADDED.format(id=new_id),
            reply_markup=keyboards.kb_admins(),
            parse_mode=enums.ParseMode.HTML,
        )

    if state == "ADD_CHANNEL" and db.is_admin(uid):
        raw = text.strip()
        chat_id_part, invite_url = raw.split("|", 1) if "|" in raw else (raw, None)
        try:
            chat = await client.get_chat(
                int(chat_id_part) if re.match(r"^-?\d+$", chat_id_part)
                else (raw if raw.startswith("@") else f"@{raw.split('t.me/')[1].split('/')[0]}")
            )
            chat_id = str(chat.id)
            chat_title = chat.title or str(chat.id)
            chat_url = invite_url or (f"https://t.me/{chat.username}" if getattr(chat, "username", None) else raw)
            db.db_add_channel(chat_id, chat_url, chat_title)
            states.user_states[uid] = {}
            return await message.reply(
                T.CHANNEL_ADDED.format(title=h(chat_title)),
                reply_markup=keyboards.kb_channels(),
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception as e:
            return await message.reply(T.ERR_CHANNEL.format(desc=h(e)), parse_mode=enums.ParseMode.HTML)

    # ── Пользовательские состояния ──
    if state == "CREATE_LINK":
        clean_text = re.sub(r"[^a-zA-Z0-9_]", "", text)

        if not clean_text or not clean_text[0].isalpha():
            return await message.reply(T.CREATE_LINK_A_Z, parse_mode=enums.ParseMode.HTML)
        if len(clean_text) < 2:
            return await message.reply(T.CREATE_LINK_SHORT, parse_mode=enums.ParseMode.HTML)

        full_link = f"{clean_text}{bot_suffix()}"
        if len(full_link) > 64:
            return await message.reply(T.CREATE_LINK_LONG, parse_mode=enums.ParseMode.HTML)

        check_msg = await message.reply(T.CREATE_LINK_CHECKING, parse_mode=enums.ParseMode.HTML)
        check_res = await tg_api("getStickerSet", data={"name": full_link})
        await check_msg.delete()

        if check_res.get("ok"):
            return await message.reply(
                T.CREATE_LINK_TAKEN.format(link=h(full_link)),
                parse_mode=enums.ParseMode.HTML,
            )

        states.user_states[uid].update({"link": full_link, "state": "CREATE_TITLE"})
        return await message.reply(T.CREATE_TITLE_OK, parse_mode=enums.ParseMode.HTML)

    if state == "CREATE_TITLE":
        states.user_states[uid].update({"title": text, "state": "PICK_EMOJI"})
        return await message.reply(T.CREATE_EMOJI, parse_mode=enums.ParseMode.HTML)

    if state == "PICK_EMOJI":
        # Пак попадёт в БД только после успешной загрузки первого медиа.
        states.user_states[uid].update({"default_emoji": text, "state": "CREATE_VECTOR"})
        mode = states.vector_prefs.get(uid, "off")
        states.user_states[uid]["vector_mode"] = mode
        return await message.reply(
            T.VECTOR_ASK,
            reply_markup=keyboards.kb_vector_mode(mode),
            parse_mode=enums.ParseMode.HTML,
        )

    # Ручной ввод ссылки для паков, созданных раньше
    if state == "MANUAL_EDIT":
        link = tg.sanitize_link(text)

        check_msg = await message.reply(T.MANUAL_CHECKING, parse_mode=enums.ParseMode.HTML)
        check_res = await tg_api("getStickerSet", data={"name": link})
        await check_msg.delete()

        if not check_res.get("ok"):
            return await message.reply(T.MANUAL_NOT_FOUND.format(name=h(link)), parse_mode=enums.ParseMode.HTML)

        title = check_res.get("result", {}).get("title", link)
        db.db_add_pack(uid, link, title)
        states.user_states[uid].update({"edit_link": link, "state": "EDIT_MENU"})
        return await message.reply(
            T.MANUAL_EDIT_DONE.format(name=h(link)),
            reply_markup=keyboards.kb_edit_pack(uid),
            parse_mode=enums.ParseMode.HTML,
        )

    if state == "EDIT_TITLE":
        new_title = text
        link = states.user_states[uid].get("edit_link")
        res = await tg_api("setStickerSetTitle", data={"name": link, "title": new_title})
        states.user_states[uid] = {}
        if res.get("ok"):
            db.db_update_pack_title(uid, link, new_title)
            await message.reply(
                T.EDIT_TITLE_DONE.format(title=h(new_title)),
                reply_markup=keyboards.MAIN_KB,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await message.reply(
                T.TG_ERROR.format(desc=h(res.get("description"))),
                reply_markup=keyboards.MAIN_KB,
                parse_mode=enums.ParseMode.HTML,
            )

    elif state == "EDIT_TRANSFER_WAIT_ID":
        source_pack = states.user_states[uid].get("edit_link")
        if not source_pack:
            states.user_states[uid] = {}
            return await message.reply(T.TRANSFER_SRC_GONE, reply_markup=keyboards.MAIN_KB)

        raw = text.strip()
        if raw.startswith("@") or not raw.lstrip("-").isdigit():
            try:
                target = await client.get_users(raw.lstrip("@"))
                recipient_id = target.id
            except Exception:
                return await message.reply(T.TRANSFER_BAD_USER)
        else:
            recipient_id = int(raw)

        if recipient_id <= 0:
            return await message.reply(T.TRANSFER_BAD_ID)
        if recipient_id == uid:
            return await message.reply(T.TRANSFER_SELF)
        if not db.db_user_known(recipient_id):
            return await message.reply(T.TRANSFER_NOT_KNOWN)

        states.user_states[uid].update({"transfer_to": recipient_id, "state": "EDIT_TRANSFER_CONFIRM"})
        return await message.reply(
            T.TRANSFER_CONFIRM.format(name=h(source_pack), uid=recipient_id),
            reply_markup=keyboards.kb_confirm_transfer(uid),
            parse_mode=enums.ParseMode.HTML,
        )

    elif state == "EDIT_THUMB":
        ce_list = [e for e in (message.entities or []) if e.type == enums.MessageEntityType.CUSTOM_EMOJI]
        if not ce_list:
            return await message.reply(T.THUMB_NOT_FROM_PACK)
        await packs.set_pack_thumb_from_emoji(client, message, uid, ce_list[0].custom_emoji_id)

    elif state == "WAIT_DELETE":
        if message.entities:
            ce_list = [e for e in message.entities if e.type == enums.MessageEntityType.CUSTOM_EMOJI]
            if ce_list:
                for ent in ce_list:
                    info = await get_custom_emoji_info(client, ent.custom_emoji_id)
                    if info and info.get("file_id"):
                        res = await tg_api("deleteStickerFromSet", data={"sticker": info["file_id"]})
                        if res.get("ok"):
                            await message.reply(T.EMOJI_DELETED, parse_mode=enums.ParseMode.HTML)
                        else:
                            await message.reply(
                                T.TG_ERROR.format(desc=h(res.get("description"))),
                                parse_mode=enums.ParseMode.HTML,
                            )
                    else:
                        await message.reply(T.NO_EMOJI_INFO)
                return

        await message.reply(T.NO_EMOJI_IN_MSG, parse_mode=enums.ParseMode.HTML)

    elif state == "WAIT_MEDIA":
        # Ссылка на чужой пак — копируем целиком
        if "addemoji/" in text or "addstickers/" in text or "t.me/" in text:
            match = re.search(r"(?:addemoji|addstickers)/([a-zA-Z0-9_]+)", text, re.IGNORECASE)
            pack_name = match.group(1) if match else text.split("/")[-1]
            await packs.copy_entire_pack(client, message, uid, pack_name)
            return

        if message.entities:
            ce_list = [e for e in message.entities if e.type == enums.MessageEntityType.CUSTOM_EMOJI]
            if ce_list:
                await packs.add_custom_emojis(client, message, uid, ce_list)
                return

        # Обычный смайлик — реакция для следующего файла
        states.user_states[uid]["next_emoji"] = text
        await message.reply(T.NEXT_EMOJI_SET.format(emoji=text), parse_mode=enums.ParseMode.HTML)


# ════════════════════════════════════════════════════════════════
#  СТИКЕРЫ
# ════════════════════════════════════════════════════════════════

@app.on_message(filters.sticker & filters.private)
async def handle_sticker(client, message: Message):
    uid = message.from_user.id
    state = states.user_states.get(uid, {}).get("state")

    if state == "WAIT_DELETE":
        res = await tg_api("deleteStickerFromSet", data={"sticker": message.sticker.file_id})
        if res.get("ok"):
            await message.reply(T.EMOJI_DELETED, parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply(
                T.EMOJI_DEL_ERR.format(desc=h(res.get("description"))),
                parse_mode=enums.ParseMode.HTML,
            )
        return

    if state == "WAIT_MEDIA":
        await packs.process_media(client, message, uid)


# ════════════════════════════════════════════════════════════════
#  МЕДИА
# ════════════════════════════════════════════════════════════════

@app.on_message((filters.photo | filters.video | filters.animation | filters.document) & filters.private)
async def handle_media(client, message: Message):
    uid = message.from_user.id
    state = states.user_states.get(uid, {}).get("state")

    if state == "EDIT_THUMB":
        await packs.set_pack_thumb(client, message, uid)
        return

    if state != "WAIT_MEDIA":
        return

    if message.document:
        mime = message.document.mime_type or ""
        fname = message.document.file_name or ""
        if not (mime.startswith("image/") or mime.startswith("video/") or fname.endswith(".tgs")):
            return await message.reply(T.UNSUPPORTED_DOC)

    await packs.process_media(client, message, uid)
