from hydrogram import enums, filters
from hydrogram.types import Message

import db
import keyboards
import states
import texts as T
import tg
from tg import app, pack_link

HTML = enums.ParseMode.HTML

# ─────────────────────────────────────────────────────────────────
#  ПЕРЕХВАТЧИК РАССЫЛКИ
#  Ловит сообщения админа во время настройки рассылки раньше всего остального.
# ─────────────────────────────────────────────────────────────────

_BCAST_STATES = {"BROADCAST_MSG", "BROADCAST_BTN_TEXT", "BROADCAST_BTN_URL", "BROADCAST_BTN_PROMPT"}


@app.on_message(filters.private, group=-1)
async def broadcast_interceptor(client, message: Message):
    uid = message.from_user.id
    if not db.is_admin(uid):
        return
    state = states.user_states.get(uid, {}).get("state")
    if state not in _BCAST_STATES:
        return

    if message.text and message.text.startswith("/"):
        states.user_states[uid] = {}
        await message.reply(T.BCAST_CANCELLED, reply_markup=keyboards.MAIN_KB)
        message.stop_propagation()
        return

    if state == "BROADCAST_BTN_PROMPT":
        # Написали текст вместо нажатия кнопки
        await message.reply(T.BCAST_BTN_NUDGE)
        message.stop_propagation()
        return

    if state == "BROADCAST_MSG":
        states.user_states[uid]["bcast_msg"] = message
        states.user_states[uid]["state"] = "BROADCAST_BTN_PROMPT"
        await message.reply(T.BCAST_BTN_Q, reply_markup=keyboards.kb_broadcast_btn_prompt())
        message.stop_propagation()
        return

    if state == "BROADCAST_BTN_TEXT":
        states.user_states[uid]["bcast_btn_text"] = message.text.strip()
        states.user_states[uid]["state"] = "BROADCAST_BTN_URL"
        await message.reply(T.BCAST_BTN_URL, parse_mode=HTML)
        message.stop_propagation()
        return

    if state == "BROADCAST_BTN_URL":
        url = message.text.strip()
        if not url.startswith(("http://", "https://", "tg://")):
            await message.reply(T.BCAST_URL_BAD, parse_mode=HTML)
            message.stop_propagation()
            return
        states.user_states[uid]["bcast_btn_url"] = url
        states.user_states[uid]["state"] = None
        from .admin import do_broadcast
        status = await message.reply(T.BCAST_LAUNCH)
        await do_broadcast(client, status, uid)
        message.stop_propagation()
        return


# ─────────────────────────────────────────────────────────────────
#  КОМАНДЫ
# ─────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private, group=-1)
async def cmd_start(client, message: Message):
    if not tg.bot_username:
        tg.bot_username = (await client.get_me()).username

    uid = message.from_user.id
    states.user_states[uid] = {}
    db.db_add_user(uid)

    ok, missing = await tg.check_subscriptions(client, uid)
    if not ok:
        return await message.reply(
            T.SUB_REQUIRED,
            reply_markup=keyboards.kb_sub_check(missing),
        )

    await message.reply(T.START, reply_markup=keyboards.MAIN_KB, parse_mode=HTML)
    message.stop_propagation()


@app.on_message(filters.command("help") & filters.private, group=-1)
async def cmd_help(client, message: Message):
    await message.reply(T.HELP, parse_mode=HTML)
    message.stop_propagation()


@app.on_message(filters.command("admin") & filters.private, group=-1)
async def cmd_admin(client, message: Message):
    if not db.is_admin(message.from_user.id):
        return await message.reply(T.ADMIN_DENIED)
    await message.reply(
        T.ADMIN_TITLE,
        reply_markup=keyboards.kb_admin_home(),
        parse_mode=HTML,
    )
    message.stop_propagation()


@app.on_message(filters.command("done") & filters.private, group=-1)
async def cmd_done(client, message: Message):
    uid = message.from_user.id
    data = states.user_states.get(uid, {})
    link = data.get("link") or data.get("edit_link") or data.get("delete_link")
    states.user_states[uid] = {}

    if link:
        await message.reply(
            T.DONE_CMD.format(link=pack_link(link)),
            reply_markup=keyboards.MAIN_KB,
            parse_mode=HTML,
        )
    else:
        await message.reply(T.DONE_PLAIN_CMD, reply_markup=keyboards.MAIN_KB)
    message.stop_propagation()


@app.on_message(filters.command("cancel") & filters.private, group=-1)
async def cmd_cancel(client, message: Message):
    states.user_states[message.from_user.id] = {}
    await message.reply(T.CANCELLED, reply_markup=keyboards.MAIN_KB)
    message.stop_propagation()
