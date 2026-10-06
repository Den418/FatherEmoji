"""Дополнительные функции: перекраска пака и выгрузка исходных файлов эмодзи."""
import asyncio
import logging

from hydrogram import enums, filters
from hydrogram.types import CallbackQuery, Message

import jobs
import keyboards
import packs
import states
import textemoji
import texts as T
from tg import app, h, tg_api

log = logging.getLogger("fatheremoji")
HTML = enums.ParseMode.HTML

MODE_NAMES = {"mono": (T.RC_MODE_MONO, T.RC_DESC_MONO), "hue": (T.RC_MODE_HUE, T.RC_DESC_HUE)}


async def open_menu(message: Message):
    await message.reply(T.EXTRA_MENU, reply_markup=keyboards.kb_extra(), parse_mode=HTML)


# ════════════════════════════════════════════════════════════════
#  ВЫБОР ФУНКЦИИ
# ════════════════════════════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^x:(recolor|file|close)$"))
async def cb_extra(client, cb: CallbackQuery):
    uid = cb.from_user.id
    action = cb.data.split(":")[1]
    await cb.answer()

    if action == "close":
        return await cb.message.delete()

    if action == "recolor":
        states.user_states[uid] = {"state": "RC_WAIT_SRC"}
        return await cb.message.edit_text(T.RC_ASK, reply_markup=keyboards.kb_cancel_only(), parse_mode=HTML)

    if action == "file":
        states.user_states[uid] = {"state": "FILE_WAIT"}
        return await cb.message.edit_text(T.FILE_ASK, reply_markup=keyboards.kb_cancel_only(), parse_mode=HTML)


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


async def on_message(client, message: Message, uid: int, state: str) -> bool:
    """Возвращает True, если сообщение обработано."""
    data = states.user_states[uid]

    if state == "FILE_WAIT":
        ents = [e for e in (message.entities or []) if e.type == enums.MessageEntityType.CUSTOM_EMOJI]
        if not ents:
            await message.reply(T.FILE_NONE)
            return True
        await packs.send_emoji_files(client, message, ents)
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
