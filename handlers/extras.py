"""Дополнительные функции: перекраска пака, скачивание эмодзи и паков, ID эмодзи и исходный код разметки."""
import asyncio
import html
import io
import logging

from hydrogram import enums, filters
from hydrogram.types import CallbackQuery, Message

import emojiid
import export
import jobs
import keyboards
import packs
import richtext
import states
import textemoji
import texts as T
from tg import app, h, tg_api

log = logging.getLogger("fatheremoji")
HTML = enums.ParseMode.HTML

MODE_NAMES = {"mono": (T.RC_MODE_MONO, T.RC_DESC_MONO), "hue": (T.RC_MODE_HUE, T.RC_DESC_HUE)}

# Состояния, в которых сообщение обрабатывается здесь
STATES = ("RC_WAIT_SRC", "RC_WAIT_HEX", "FILE_WAIT", "ID_WAIT")
# Там, где источник это эмодзи, стикер, пересланный пост или подпись к медиа
SOURCE_STATES = ("RC_WAIT_SRC", "FILE_WAIT", "ID_WAIT")


async def open_menu(message: Message):
    await message.reply(T.EXTRA_MENU, reply_markup=keyboards.kb_extra(), parse_mode=HTML)


# ════════════════════════════════════════════════════════════════
#  ВЫБОР ФУНКЦИИ
# ════════════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^x:(recolor|file|ids|close)$"))
async def cb_extra(client, cb: CallbackQuery):
    uid = cb.from_user.id
    action = cb.data.split(":")[1]
    await cb.answer()

    if action == "close":
        return await cb.message.delete()

    texts = {"recolor": ("RC_WAIT_SRC", T.RC_ASK), "file": ("FILE_WAIT", T.FILE_ASK), "ids": ("ID_WAIT", T.IDS_ASK)}
    state, text = texts[action]
    states.user_states[uid] = {"state": state}
    await cb.message.edit_text(text, reply_markup=keyboards.kb_cancel_only(), parse_mode=HTML)


# ════════════════════════════════════════════════════════════════
#  СООБЩЕНИЯ В СОСТОЯНИЯХ ДОП. ФУНКЦИЙ
# ════════════════════════════════════════════════════════════════

async def _show_color_picker(message: Message, uid: int):
    rc = states.user_states[uid]["rc"]
    name, desc = MODE_NAMES[rc["mode"]]
    await message.reply(
        T.RC_PICK.format(title=h(rc["title"]), count=rc["count"], mode=name, desc=desc),
        reply_markup=keyboards.kb_recolor(rc["mode"]),
        parse_mode=HTML,
    )


async def _offer_pack(message: Message, uid: int, info: dict):
    """Панель выбора, что положить в ZIP целого пака."""
    fmt, n = info["fmt"], info["count"]
    est = lambda mode: jobs.fmt_duration(export.estimate(mode, fmt, n))
    states.user_states.setdefault(uid, {})["pk"] = info
    await message.reply(
        T.PK_PANEL.format(title=h(info["title"]), count=n, what=T.PK_WHAT.get(info["type"], "эмодзи"),
                          fmt=T.PK_FMT[fmt], t_all=est("all"), t_orig=est("orig"), t_png=est("png"), t_gif=est("gif")),
        reply_markup=keyboards.kb_pack_mode(), parse_mode=HTML)


async def _send_source(message: Message, text: str, entities: list):
    """Исходный код сообщения в HTML и MarkdownV2 (для вставки в своего бота)."""
    if not text.strip():
        return
    src_html = richtext.to_html(text, entities)
    src_md = richtext.to_markdown(text, entities)
    body = T.IDS_SOURCE.format(html=html.escape(src_html, quote=False), md=html.escape(src_md, quote=False))
    if len(body) <= 3900:
        return await message.reply(body, parse_mode=HTML, disable_web_page_preview=True)
    buf = io.BytesIO(f"HTML\n{src_html}\n\nMarkdownV2\n{src_md}\n".encode("utf-8"))
    buf.name = "message_source.txt"
    await message.reply_document(buf, caption=T.IDS_SOURCE_FILE)


async def _ids(client, message: Message):
    """Функция «ID эмодзи»: эмодзи, стикер или пак, плюс исходный код разметки."""
    text, ents = richtext.body(message)

    ce = richtext.custom_emojis(message)
    if ce:
        lines = [emojiid.id_line(ent.custom_emoji_id, chars) for ent, chars in ce]
        for chunk in emojiid.split_message("\n".join(lines)):
            await message.reply(chunk, parse_mode=HTML)
        return await _send_source(message, text, ents)

    sticker = getattr(message, "sticker", None)
    if sticker:
        found = await packs.sticker_id(sticker)
        if found:
            return await message.reply(emojiid.id_line(found[1], found[0]), parse_mode=HTML)

    name, origin = await packs.resolve_pack_name(client, message)
    if name:
        got = await packs.pack_ids(name)
        if got and got[1]:
            title, rows = got
            if len(rows) <= 30:
                lines = [emojiid.id_line(i, ch) for ch, i in rows]
                for chunk in emojiid.split_message("\n".join(lines)):
                    await message.reply(chunk, parse_mode=HTML)
                return
            buf = io.BytesIO(emojiid.table(rows).encode("utf-8"))
            buf.name = "emoji_ids.txt"
            return await message.reply_document(buf, caption=T.IDS_PACK_CAPTION.format(title=h(title), count=len(rows)),
                                                parse_mode=HTML)

    if richtext.has_markup(message):
        return await _send_source(message, text, ents)
    await message.reply(T.IDS_NONE, parse_mode=HTML)


async def on_message(client, message: Message, uid: int, state: str) -> bool:
    """Возвращает True, если сообщение обработано."""
    data = states.user_states[uid]

    if state == "FILE_WAIT":
        ce = richtext.custom_emojis(message)
        if ce:
            await packs.send_emoji_files(client, message, [ent for ent, _ in ce])
            return True
        sticker = getattr(message, "sticker", None)
        if sticker:
            await packs.send_sticker_files(client, message)
            info = await packs.pack_info(sticker.set_name) if getattr(sticker, "set_name", None) else None
            if info:
                await _offer_pack(message, uid, info)       # заодно предлагаем скачать весь пак этого стикера
            return True
        name, _ = await packs.resolve_pack_name(client, message)
        info = await packs.pack_info(name) if name else None
        if info:
            await _offer_pack(message, uid, info)
        else:
            await message.reply(T.FILE_NONE, parse_mode=HTML)
        return True

    if state == "ID_WAIT":
        await _ids(client, message)
        return True

    if state == "RC_WAIT_SRC":
        name, origin = await packs.resolve_pack_name(client, message)
        if origin == "hidden":
            await message.reply(T.RC_HIDDEN)
            return True
        if not name:
            await message.reply(T.RC_NO_SRC, parse_mode=HTML)
            return True
        res = await tg_api("getStickerSet", data={"name": name})
        if not res.get("ok"):
            await message.reply(T.CLONE_NOT_FOUND, parse_mode=HTML)
            return True
        result = res["result"]
        data["rc"] = {
            "src": name,
            "title": result.get("title", name),
            "count": len(result.get("stickers", [])),
            "mode": states.recolor_prefs.get(uid, "mono"),
        }
        data["state"] = "RC_PICK"
        await _show_color_picker(message, uid)
        return True

    if state == "RC_WAIT_HEX":
        hx = textemoji.parse_hex(message.text or "")
        if not hx:
            await message.reply(T.TXT_HEX_BAD, parse_mode=HTML)
            return True
        await _run(client, message, uid, hx)
        return True

    return False


# ════════════════════════════════════════════════════════════════
#  КНОПКИ ПЕРЕКРАСКИ
# ════════════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^rc:"))
async def cb_recolor(client, cb: CallbackQuery):
    uid = cb.from_user.id
    data = states.user_states.get(uid, {})
    rc = data.get("rc")
    if not rc:
        return await cb.answer(T.RC_STALE, show_alert=True)

    parts = cb.data.split(":")
    action = parts[1]

    if action == "mode":
        rc["mode"] = parts[2]
        states.recolor_prefs[uid] = parts[2]
        name, desc = MODE_NAMES[rc["mode"]]
        await cb.answer()
        return await cb.message.edit_text(
            T.RC_PICK.format(title=h(rc["title"]), count=rc["count"], mode=name, desc=desc),
            reply_markup=keyboards.kb_recolor(rc["mode"]), parse_mode=HTML)

    if action == "hex":
        data["state"] = "RC_WAIT_HEX"
        await cb.answer()
        return await cb.message.reply(T.RC_HEX_ASK, parse_mode=HTML)

    if action == "c":
        await cb.answer()
        await _run(client, cb.message, uid, "#" + parts[2], edit=True)


_tasks: set = set()          # ссылки на фоновые задачи, чтобы их не убрал сборщик мусора


async def _run(client, message: Message, uid: int, hex_color: str, edit: bool = False):
    data = states.user_states.get(uid, {})
    rc = data.get("rc")
    if not rc:
        return await message.reply(T.RC_STALE)
    if jobs.busy(uid):
        return await message.reply(T.JOB_BUSY)

    states.user_states[uid] = {}          # сессия закончена, чтобы кнопки не нажали повторно
    kb = keyboards.kb_job_stop()
    status = await (message.edit_text(T.RC_START, reply_markup=kb) if edit else message.reply(T.RC_START, reply_markup=kb))
    job = jobs.Job(uid, status, rc["count"], head=T.RC_HEAD, extra=T.RC_EXTRA.format(color=hex_color), pacing=True)
    states.jobs[uid] = job                # место занято сразу, до старта задачи

    # Перекраска большого пака идёт часами (паузы между пачками), поэтому не держим обработчик апдейта
    task = asyncio.create_task(_job(client, status, uid, rc, hex_color, job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _job(client, status: Message, uid: int, rc: dict, hex_color: str, job: "jobs.Job"):
    try:
        await packs.recolor_pack(client, job, rc["src"], hex_color, rc["mode"])
    except Exception as e:
        log.error(f"recolor job: {e}", exc_info=True)
        await job.finish(T.ERR_UNKNOWN)
    finally:
        states.jobs.pop(uid, None)
    try:
        await status.reply(T.MENU_NEXT, reply_markup=keyboards.MAIN_KB)
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^job:stop$"))
async def cb_job_stop(client, cb: CallbackQuery):
    job = states.jobs.get(cb.from_user.id)
    if not job:
        return await cb.answer(T.JOB_NONE, show_alert=True)
    job.cancel = True
    await cb.answer(T.JOB_STOPPING)


# ════════════════════════════════════════════════════════════════
#  СКАЧИВАНИЕ ЦЕЛОГО ПАКА
# ════════════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^pk:(all|orig|png|gif|close)$"))
async def cb_pack(client, cb: CallbackQuery):
    uid = cb.from_user.id
    mode = cb.data.split(":")[1]
    if mode == "close":
        await cb.answer()
        return await cb.message.delete()

    data = states.user_states.get(uid, {})
    pk = data.get("pk")
    if not pk:
        return await cb.answer(T.PK_STALE, show_alert=True)
    if jobs.busy(uid):
        return await cb.answer(T.JOB_BUSY, show_alert=True)
    await cb.answer()

    data.pop("pk", None)
    data["state"] = "FILE_WAIT"          # после архива можно сразу присылать следующие эмодзи
    status = await cb.message.edit_text(T.PK_START, reply_markup=keyboards.kb_job_stop())
    job = jobs.Job(uid, status, pk["count"], head=T.PK_HEAD,
                   extra=T.PK_EXTRA.format(title=h(pk["title"]), mode=T.PK_MODE_NAMES[mode]))
    # первая оценка по таблице, дальше её заменяют реальные замеры
    job.proc_time, job.proc_n = export.EST_SECONDS[mode][pk["fmt"]], 1
    states.jobs[uid] = job

    task = asyncio.create_task(_pack_job(client, uid, pk["name"], mode, job))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _pack_job(client, uid: int, name: str, mode: str, job: "jobs.Job"):
    try:
        await packs.download_pack_zip(client, job, name, mode)
    except Exception as e:
        log.error(f"pack zip job: {e}", exc_info=True)
        await job.finish(T.ERR_UNKNOWN)
    finally:
        states.jobs.pop(uid, None)
