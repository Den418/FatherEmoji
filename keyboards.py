from hydrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

import config
import db
import texts as T

MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(T.BTN_CREATE), KeyboardButton(T.BTN_EDIT)],
        [KeyboardButton(T.BTN_CHANNEL), KeyboardButton(T.BTN_HELP)],
    ],
    resize_keyboard=True,
)

SESSION_KB = ReplyKeyboardMarkup(
    [[KeyboardButton(T.BTN_DONE)]],
    resize_keyboard=True,
)


def kb_user_packs(uid: int) -> InlineKeyboardMarkup:
    packs = db.db_get_packs(uid)
    rows = []
    for pid, name, title in packs:
        rows.append([InlineKeyboardButton(f"📦 {title}", callback_data=f"p:edit:{pid}")])
    rows.append([InlineKeyboardButton(T.IB_MANUAL, callback_data="p_manual:edit")])
    rows.append([InlineKeyboardButton(T.IB_CANCEL, callback_data="action:cancel")])
    return InlineKeyboardMarkup(rows)


def kb_sub_check(missing: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📢 {title}", url=url)] for title, url in missing]
    rows.append([InlineKeyboardButton(T.IB_CHECK_SUB, callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)


def kb_admin_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_STATS, callback_data="adm:stats")],
        [
            InlineKeyboardButton(T.IB_BCAST,  callback_data="adm:broadcast"),
            InlineKeyboardButton(T.IB_EXPORT, callback_data="adm:export"),
        ],
        [InlineKeyboardButton(T.IB_ADMINS,   callback_data="adm:admins")],
        [InlineKeyboardButton(T.IB_CHANNELS, callback_data="adm:channels")],
    ])


def kb_admins() -> InlineKeyboardMarkup:
    admins = db.db_get_admins()
    rows = []
    for aid in admins:
        label = (T.ADMIN_MAIN if aid == config.SUPER_ADMIN else "") + f"ID: {aid}"
        if aid != config.SUPER_ADMIN:
            rows.append([
                InlineKeyboardButton(label, callback_data="noop"),
                InlineKeyboardButton(T.IB_REMOVE, callback_data=f"adm:del_admin:{aid}"),
            ])
        else:
            rows.append([InlineKeyboardButton(label, callback_data="noop")])
    rows.append([InlineKeyboardButton(T.IB_ADD_ADMIN, callback_data="adm:add_admin")])
    rows.append([InlineKeyboardButton(T.IB_BACK, callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def kb_channels() -> InlineKeyboardMarkup:
    channels = db.db_get_channels()
    rows = []
    for row_id, _, chat_url, chat_title in channels:
        rows.append([
            InlineKeyboardButton(f"📡 {chat_title}", url=chat_url),
            InlineKeyboardButton(T.IB_REMOVE, callback_data=f"adm:del_channel:{row_id}"),
        ])
    rows.append([InlineKeyboardButton(T.IB_ADD_CHANNEL, callback_data="adm:add_channel")])
    rows.append([InlineKeyboardButton(T.IB_BACK, callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def kb_edit_pack(uid: int) -> InlineKeyboardMarkup:
    """Меню управления паком: добавление/удаление эмодзи и остальные действия."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_ADD_EMOJIS, callback_data=f"edit:add_emojis:{uid}")],
        [InlineKeyboardButton(T.IB_DEL_EMOJIS, callback_data=f"edit:del_emojis:{uid}")],
        [InlineKeyboardButton(T.IB_RENAME,    callback_data=f"edit:rename:{uid}")],
        [InlineKeyboardButton(T.IB_THUMB,     callback_data=f"edit:thumb:{uid}")],
        [InlineKeyboardButton(T.IB_TRANSFER,  callback_data=f"edit:transfer:{uid}")],
        [InlineKeyboardButton(T.IB_DEL_PACK,  callback_data=f"edit:delete_pack:{uid}")],
        [InlineKeyboardButton(T.IB_CANCEL,    callback_data=f"edit:cancel:{uid}")],
    ])


def kb_edit_thumb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_THUMB_RESET, callback_data=f"edit:thumb_reset:{uid}")],
        [InlineKeyboardButton(T.IB_CANCEL,      callback_data=f"edit:cancel:{uid}")],
    ])


def kb_confirm_delete(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_DEL_YES, callback_data=f"edit:do_delete:{uid}")],
        [InlineKeyboardButton(T.IB_DEL_NO,  callback_data=f"edit:cancel:{uid}")],
    ])


def kb_confirm_transfer(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_TR_YES, callback_data=f"edit:do_transfer:{uid}")],
        [InlineKeyboardButton(T.IB_TR_NO,  callback_data=f"edit:cancel:{uid}")],
    ])


def kb_back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_BACK, callback_data="adm:home")]
    ])


def kb_done_action() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_DONE, callback_data="action:done")]
    ])


def kb_broadcast_btn_prompt() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(T.IB_ADD_BTN, callback_data="adm:bcast_add_btn"),
            InlineKeyboardButton(T.IB_NO_BTN,  callback_data="adm:bcast_no_btn"),
        ]
    ])


def kb_cancel_to(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(T.IB_BACK, callback_data=cb)]])


# ── Переключалка векторизации ──

def kb_vector_mode(mode: str) -> InlineKeyboardMarkup:
    """Три режима; активный помечается галочкой."""
    def label(active: bool, text: str) -> str:
        return (T.IB_VEC_MARK + text) if active else text
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label(mode == "off", T.IB_VEC_OFF), callback_data="vecmode:off")],
        [InlineKeyboardButton(label(mode == "on",  T.IB_VEC_ON),  callback_data="vecmode:on")],
        [InlineKeyboardButton(label(mode == "ask", T.IB_VEC_ASK), callback_data="vecmode:ask")],
    ])


def kb_vec_ask() -> InlineKeyboardMarkup:
    """Вопрос к конкретной картинке: вектор или обычная."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(T.IB_VEC_V, callback_data="vecask:v"),
            InlineKeyboardButton(T.IB_VEC_N, callback_data="vecask:n"),
        ],
    ])
