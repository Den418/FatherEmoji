import logging

from hydrogram import enums, filters
from hydrogram.types import CallbackQuery

import db
import keyboards
import packs
import states
import texts as T
import tg
from tg import app, h, pack_link, tg_api

log = logging.getLogger("fatheremoji")

HTML = enums.ParseMode.HTML


async def _pack_header_data(pack_name: str) -> dict:
    """Количество эмодзи и шапка со ссылкой для экранов выбранного пака."""
    pack_res = await tg_api("getStickerSet", data={"name": pack_name})
    count = len(pack_res.get("result", {}).get("stickers", [])) if pack_res.get("ok") else "?"
    return dict(count=count, link=pack_link(pack_name))


@app.on_callback_query(filters.regex(r"^action:done$"))
async def cb_action_done(client, cb: CallbackQuery):
    uid = cb.from_user.id
    data = states.user_states.get(uid, {})
    link = data.get("link") or data.get("edit_link") or data.get("delete_link")
    states.user_states[uid] = {}

    await cb.answer()
    if link:
        await cb.message.edit_text(
            T.DONE_CB.format(link=pack_link(link)),
            disable_web_page_preview=True,
            parse_mode=HTML,
        )
    else:
        await cb.message.edit_text(T.DONE_PLAIN)
    await cb.message.reply(T.MENU_NEXT, reply_markup=keyboards.MAIN_KB)


@app.on_callback_query(filters.regex(r"^action:cancel$"))
async def cb_action_cancel(client, cb: CallbackQuery):
    states.user_states[cb.from_user.id] = {}
    await cb.answer()
    await cb.message.delete()
    await cb.message.reply(T.CANCELLED, reply_markup=keyboards.MAIN_KB)


@app.on_callback_query(filters.regex(r"^check_sub$"))
async def cb_check_sub(client, cb: CallbackQuery):
    uid = cb.from_user.id
    ok, missing = await tg.check_subscriptions(client, uid)
    if not ok:
        await cb.answer(T.SUB_MISSING, show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=keyboards.kb_sub_check(missing))
    else:
        await cb.answer()
        await cb.message.delete()
        await cb.message.reply(T.SUB_OK, reply_markup=keyboards.MAIN_KB, parse_mode=HTML)


# ── Выбор пака из списка (редактирование) ──

@app.on_callback_query(filters.regex(r"^p:edit:(\d+)$"))
async def cb_pack_select(client, cb: CallbackQuery):
    uid = cb.from_user.id
    pack_id = int(cb.matches[0].group(1))

    pack_data = db.db_get_pack_by_id(pack_id)
    if not pack_data:
        return await cb.answer(T.PACK_NOT_IN_DB, show_alert=True)

    pack_name, pack_title = pack_data
    header = await _pack_header_data(pack_name)

    states.user_states.setdefault(uid, {}).update({"edit_link": pack_name, "state": "EDIT_MENU"})
    await cb.message.edit_text(
        T.PACK_MENU_HEADER.format(title=h(pack_title), **header),
        reply_markup=keyboards.kb_edit_pack(uid),
        disable_web_page_preview=True,
        parse_mode=HTML,
    )
    await cb.answer()


@app.on_callback_query(filters.regex(r"^p_manual:edit$"))
async def cb_pack_manual(client, cb: CallbackQuery):
    states.user_states[cb.from_user.id] = {"state": "MANUAL_EDIT"}
    await cb.message.edit_text(T.MANUAL_ASK, parse_mode=HTML)
    await cb.answer()


# ── Меню пака: добавление/удаление эмодзи и остальные действия ──

@app.on_callback_query(
    filters.regex(r"^edit:(add_emojis|del_emojis|rename|thumb|thumb_reset|cancel|info|delete_pack|do_delete|transfer|do_transfer):\d+$")
)
async def cb_edit(client, cb: CallbackQuery):
    parts = cb.data.split(":")
    action = parts[1]
    target_uid = int(parts[2])

    if cb.from_user.id != target_uid:
        return await cb.answer(T.NOT_YOUR_MENU, show_alert=True)

    if action == "add_emojis":
        pack_name = states.user_states.get(target_uid, {}).get("edit_link")
        if not pack_name:
            return await cb.answer(T.PACK_GONE, show_alert=True)
        await cb.answer()
        header = await _pack_header_data(pack_name)
        mode = states.vector_prefs.get(target_uid, "off")
        note = T.VECTOR_FILL_ON_NOTE if mode == "on" else (T.VECTOR_FILL_ASK_NOTE if mode == "ask" else "")
        pack_title = db.db_get_pack_title(target_uid, pack_name) or pack_name
        states.user_states[target_uid].update(
            {"link": pack_name, "title": pack_title, "state": "WAIT_MEDIA",
             "default_emoji": "✨", "vector_mode": mode}
        )
        await cb.message.edit_text(
            T.PACK_FILL_HEADER.format(title=h(pack_title), **header) + note,
            reply_markup=keyboards.kb_vector_mode(mode),
            disable_web_page_preview=True,
            parse_mode=HTML,
        )
        await cb.message.reply(T.SESSION_ADD_HINT, reply_markup=keyboards.SESSION_KB)

    elif action == "del_emojis":
        pack_name = states.user_states.get(target_uid, {}).get("edit_link")
        if not pack_name:
            return await cb.answer(T.PACK_GONE, show_alert=True)
        await cb.answer()
        header = await _pack_header_data(pack_name)
        pack_title = db.db_get_pack_title(target_uid, pack_name) or pack_name
        states.user_states[target_uid].update({"delete_link": pack_name, "state": "WAIT_DELETE"})
        await cb.message.edit_text(
            T.PACK_DEL_HEADER.format(title=h(pack_title), **header),
            disable_web_page_preview=True,
            parse_mode=HTML,
        )
        await cb.message.reply(T.SESSION_DEL_HINT, reply_markup=keyboards.SESSION_KB)

    elif action == "rename":
        states.user_states.setdefault(target_uid, {})["state"] = "EDIT_TITLE"
        await cb.message.edit_text(T.EDIT_ASK_TITLE)
    elif action == "thumb":
        states.user_states.setdefault(target_uid, {})["state"] = "EDIT_THUMB"
        await cb.message.edit_text(
            T.EDIT_THUMB_EXPLAIN,
            reply_markup=keyboards.kb_edit_thumb(target_uid),
            parse_mode=HTML,
        )
    elif action == "thumb_reset":
        link = states.user_states.get(target_uid, {}).get("edit_link")
        if not link:
            return await cb.answer(T.PACK_GONE, show_alert=True)
        await cb.answer()
        res = await tg_api("setCustomEmojiStickerSetThumbnail", data={"name": link, "custom_emoji_id": ""})
        states.user_states[target_uid] = {}
        if res.get("ok"):
            await cb.message.edit_text(T.THUMB_RESET_DONE, parse_mode=HTML)
            await cb.message.reply(T.MENU_NEXT, reply_markup=keyboards.MAIN_KB)
        else:
            await cb.message.edit_text(
                T.TG_ERROR.format(desc=h(res.get("description"))),
                parse_mode=HTML,
            )
    elif action == "info":
        pack_name = states.user_states.get(target_uid, {}).get("edit_link")
        if not pack_name:
            return await cb.answer(T.PACK_GONE, show_alert=True)
        await cb.answer()
        res = await tg_api("getStickerSet", data={"name": pack_name})
        if res.get("ok"):
            result = res["result"]
            count = len(result.get("stickers", []))
            title = result.get("title", pack_name)
            await cb.message.edit_text(
                T.PACK_INFO.format(title=h(title), name=h(pack_name), count=count, link=pack_link(pack_name)),
                reply_markup=keyboards.kb_edit_pack(target_uid),
                disable_web_page_preview=True,
                parse_mode=HTML,
            )
        else:
            await cb.message.reply(
                T.TG_ERROR.format(desc=h(res.get("description"))),
                reply_markup=keyboards.kb_edit_pack(target_uid),
                parse_mode=HTML,
            )
    elif action == "delete_pack":
        pack_name = states.user_states.get(target_uid, {}).get("edit_link", "")
        await cb.message.edit_text(
            T.DELETE_CONFIRM.format(name=h(pack_name)),
            reply_markup=keyboards.kb_confirm_delete(target_uid),
            parse_mode=HTML,
        )
    elif action == "do_delete":
        pack_name = states.user_states.get(target_uid, {}).get("edit_link")
        if not pack_name:
            return await cb.answer(T.PACK_GONE, show_alert=True)
        await cb.answer()
        res = await tg_api("deleteStickerSet", data={"name": pack_name})
        if res.get("ok"):
            db.db_remove_pack(target_uid, pack_name)
            states.user_states[target_uid] = {}
            log.info(f"Пак {pack_name} удалён пользователем {target_uid}")
            await cb.message.edit_text(T.PACK_DELETED, parse_mode=HTML)
            await cb.message.reply(T.MENU_NEXT, reply_markup=keyboards.MAIN_KB)
        else:
            desc = res.get("description", "")
            await cb.message.reply(
                T.TG_ERROR.format(desc=h(desc)),
                reply_markup=keyboards.kb_edit_pack(target_uid),
                parse_mode=HTML,
            )
    elif action == "transfer":
        states.user_states.setdefault(target_uid, {})["state"] = "EDIT_TRANSFER_WAIT_ID"
        await cb.message.edit_text(T.TRANSFER_ASK, parse_mode=HTML)
    elif action == "do_transfer":
        data = states.user_states.get(target_uid, {})
        source_pack = data.get("edit_link")
        recipient_id = data.get("transfer_to")
        if not source_pack or not recipient_id:
            return await cb.answer(T.TRANSFER_STALE, show_alert=True)
        await cb.answer()
        states.user_states[target_uid] = {}
        await packs.transfer_pack(client, cb.message, target_uid, recipient_id, source_pack)
    elif action == "cancel":
        states.user_states[target_uid] = {}
        await cb.answer()
        await cb.message.edit_text(T.EDIT_CANCELLED, parse_mode=HTML)
        await cb.message.reply(T.MENU_NEXT, reply_markup=keyboards.MAIN_KB)


# ── Переключалка векторизации ──

@app.on_callback_query(filters.regex(r"^vecmode:(off|on|ask)$"))
async def cb_vecmode(client, cb: CallbackQuery):
    uid = cb.from_user.id
    mode = cb.matches[0].group(1)

    data = states.user_states.setdefault(uid, {})
    data["vector_mode"] = mode
    states.vector_prefs[uid] = mode

    await cb.answer(T.VECTOR_MODE_SET)

    if data.get("state") == "CREATE_VECTOR":
        # Финальный шаг мастера создания пака
        data["state"] = "WAIT_MEDIA"
        await cb.message.edit_text(
            T.CREATE_READY,
            reply_markup=keyboards.kb_vector_mode(mode),
            parse_mode=HTML,
        )
        await cb.message.reply(T.SESSION_ADD_HINT, reply_markup=keyboards.SESSION_KB)
    else:
        desc = {"off": T.VECTOR_DESC_OFF, "on": T.VECTOR_DESC_ON, "ask": T.VECTOR_DESC_ASK}[mode]
        await cb.message.edit_text(
            T.VECTOR_MODE_TITLE.format(desc=desc),
            reply_markup=keyboards.kb_vector_mode(mode),
            parse_mode=HTML,
        )


# ── Ответ на вопрос «вектор или обычная?» ──

@app.on_callback_query(filters.regex(r"^vecask:(v|n)$"))
async def cb_vecask(client, cb: CallbackQuery):
    uid = cb.from_user.id
    vector = cb.matches[0].group(1) == "v"

    data = states.user_states.get(uid, {})
    ask_msg = data.pop("ask_msg", None)
    if ask_msg is None or data.get("state") != "WAIT_MEDIA":
        return await cb.answer(T.ASK_STALE, show_alert=True)

    await cb.answer()
    # Убираем кнопки у вопроса, чтобы не нажать повторно
    try:
        await cb.message.edit_reply_markup()
    except Exception:
        pass
    await packs.process_media(client, ask_msg, uid, force_vector=vector)


@app.on_callback_query(filters.regex(r"^noop$"))
async def cb_noop(client, cb: CallbackQuery):
    await cb.answer()
