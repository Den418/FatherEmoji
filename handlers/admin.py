import asyncio
import os
import tempfile
from datetime import datetime

from hydrogram import enums, filters
from hydrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import db
import keyboards
import states
import texts as T
from tg import app

HTML = enums.ParseMode.HTML


async def do_broadcast(client, status_msg, uid: int):
    data = states.user_states.pop(uid, {})
    bcast_msg = data.get("bcast_msg")
    btn_text = data.get("bcast_btn_text")
    btn_url = data.get("bcast_btn_url")

    reply_markup = None
    if btn_text and btn_url:
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])

    users = db.db_all_users()
    ok_count = fail_count = 0

    await status_msg.edit_text(
        T.BCAST_START.format(total=len(users)),
        parse_mode=HTML,
    )

    for i, target_id in enumerate(users):
        try:
            await bcast_msg.copy(chat_id=target_id, reply_markup=reply_markup)
            ok_count += 1
        except Exception:
            fail_count += 1

        if (i + 1) % 25 == 0:
            try:
                await status_msg.edit_text(
                    T.BCAST_PROGRESS.format(done=i + 1, total=len(users)),
                    parse_mode=HTML,
                )
            except Exception:
                pass

        if (i + 1) % 30 == 0:
            await asyncio.sleep(1)

    await status_msg.edit_text(
        T.BCAST_DONE.format(ok=ok_count, fail=fail_count, total=len(users)),
        parse_mode=HTML,
    )


@app.on_callback_query(filters.regex(r"^adm:"))
async def cb_admin(client, cb: CallbackQuery):
    uid = cb.from_user.id
    if not db.is_admin(uid):
        return await cb.answer(T.ADMIN_NO_ACCESS, show_alert=True)
    action = cb.data

    if action == "adm:home":
        await cb.message.edit_text(T.ADMIN_TITLE, reply_markup=keyboards.kb_admin_home(), parse_mode=HTML)
    elif action == "adm:stats":
        await cb.message.edit_text(
            T.STATS.format(
                users=db.db_user_count(),
                admins=len(db.db_get_admins()),
                channels=len(db.db_get_channels()),
            ),
            reply_markup=keyboards.kb_back_home(),
            parse_mode=HTML,
        )
    elif action == "adm:broadcast":
        states.user_states[uid] = {"state": "BROADCAST_MSG"}
        await cb.message.edit_text(T.BCAST_ASK, parse_mode=HTML)
    elif action == "adm:bcast_add_btn":
        if states.user_states.get(uid, {}).get("state") != "BROADCAST_BTN_PROMPT":
            return await cb.answer()
        states.user_states[uid]["state"] = "BROADCAST_BTN_TEXT"
        await cb.message.edit_text(T.BCAST_BTN_TEXT, parse_mode=HTML)
    elif action == "adm:bcast_no_btn":
        if states.user_states.get(uid, {}).get("state") != "BROADCAST_BTN_PROMPT":
            return await cb.answer()
        states.user_states[uid]["state"] = None
        await cb.message.edit_text(T.BCAST_LAUNCH)
        await do_broadcast(client, cb.message, uid)
    elif action == "adm:export":
        users = db.db_all_users()
        fname = os.path.join(tempfile.gettempdir(), f"users_{int(datetime.now().timestamp())}.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write("\n".join(str(u) for u in users))
        await cb.message.reply_document(
            document=fname,
            caption=T.EXPORT_CAPTION.format(
                n=len(users),
                date=datetime.now().strftime("%d.%m.%Y %H:%M"),
            ),
            parse_mode=HTML,
        )
        os.remove(fname)
        await cb.answer(T.EXPORT_ANSWER)
    elif action == "adm:admins":
        await cb.message.edit_text(T.ADMINS_TITLE, reply_markup=keyboards.kb_admins(), parse_mode=HTML)
    elif action == "adm:add_admin":
        states.user_states[uid] = {"state": "ADD_ADMIN"}
        await cb.message.edit_text(
            T.ADMIN_ASK_ID,
            reply_markup=keyboards.kb_cancel_to("adm:admins"),
            parse_mode=HTML,
        )
    elif action.startswith("adm:del_admin:"):
        target = int(action.split(":")[-1])
        if db.db_remove_admin(target):
            await cb.answer(T.ADMIN_REMOVED.format(id=target))
            await cb.message.edit_text(T.ADMINS_TITLE, reply_markup=keyboards.kb_admins(), parse_mode=HTML)
        else:
            await cb.answer(T.ADMIN_KEEP_MAIN, show_alert=True)
    elif action == "adm:channels":
        await cb.message.edit_text(T.CHANNELS_TITLE, reply_markup=keyboards.kb_channels(), parse_mode=HTML)
    elif action == "adm:add_channel":
        states.user_states[uid] = {"state": "ADD_CHANNEL"}
        await cb.message.edit_text(
            T.CHANNEL_ASK,
            reply_markup=keyboards.kb_cancel_to("adm:channels"),
            parse_mode=HTML,
        )
    elif action.startswith("adm:del_channel:"):
        row_id = int(action.split(":")[-1])
        db.db_remove_channel(row_id)
        await cb.answer(T.CHANNEL_REMOVED)
        await cb.message.edit_text(T.CHANNELS_TITLE, reply_markup=keyboards.kb_channels(), parse_mode=HTML)
