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
        [KeyboardButton(T.BTN_EXTRA)],
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


# ── Общая палитра для текстовых эмодзи и перекраски ──

COLORS = [
    ("⬛", "#1C1C1E"), ("⬜", "#FFFFFF"), ("🟥", "#E5393B"), ("🟧", "#FF8A1F"), ("🟨", "#FFD426"),
    ("🟩", "#2DB84D"), ("🟦", "#2F7BFF"), ("🟪", "#8E44E8"), ("🩷", "#FF5FA2"), ("🟫", "#8B5A3C"),
]


def color_name(hex_color: str | None) -> str:
    """Подпись цвета для сообщений: квадратик, если цвет из палитры, и сам HEX."""
    if not hex_color:
        return T.TXT_NO_BG
    for emo, hx in COLORS:
        if hx.upper() == hex_color.upper():
            return f"{emo} {hex_color}"
    return hex_color


def _palette_rows(prefix: str, current: str | None):
    rows, row = [], []
    for emo, hx in COLORS:
        label = (T.IB_TX_MARK + emo) if current and hx.upper() == current.upper() else emo
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{hx.lstrip('#')}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return rows


# ── Выбор режима наполнения ──

def kb_fill_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_MODE_NORMAL, callback_data="fill:normal")],
        [InlineKeyboardButton(T.IB_MODE_TEXT,   callback_data="fill:text")],
    ])


# ── Редактор текстового эмодзи ──

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def kb_text_main(fmt_label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(T.IB_TX_BG, "tx:menu:bg"), _btn(T.IB_TX_FG, "tx:menu:fg")],
        [_btn(T.IB_TX_FONT, "tx:menu:font"), _btn(T.IB_TX_SHAPE, "tx:menu:shape")],
        [_btn(T.IB_TX_FX, "tx:menu:fx"), _btn(T.IB_TX_ANIM, "tx:menu:anim")],
        [_btn(T.IB_TX_FMT.format(fmt=fmt_label), "tx:menu:fmt")],
        [_btn(T.IB_TX_ADD, "tx:add")],
    ])


# Куда ведёт кнопка «Назад» из экрана выбора цвета
COLOR_BACK = {"bg": "main", "fg": "main", "gc": "fx", "oc": "fx", "g2": "fx", "b2": "fx"}
COLOR_RESET = {"bg": T.IB_TX_NOBG, "gc": T.IB_TX_SAME, "oc": T.IB_TX_AUTO}


def kb_text_colors(which: str, current: str | None) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for emo, hx in COLORS:
        label = (T.IB_TX_MARK + emo) if current and hx.upper() == current.upper() else emo
        row.append(_btn(label, f"tx:col:{which}:{hx.lstrip('#')}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)
    extra = [_btn(T.IB_TX_HEX, f"tx:hex:{which}")]
    if which in COLOR_RESET:
        extra.insert(0, _btn((T.IB_TX_MARK if current is None else "") + COLOR_RESET[which], f"tx:col:{which}:none"))
    rows.append(extra)
    rows.append([_btn(T.IB_TX_BACK, f"tx:menu:{COLOR_BACK[which]}")])
    return InlineKeyboardMarkup(rows)


def kb_text_fonts(current: str) -> InlineKeyboardMarkup:
    import textemoji
    rows, row = [], []
    for key, (_, label) in textemoji.FONTS.items():
        row.append(_btn((T.IB_TX_MARK if key == current else "") + label, f"tx:font:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([_btn(T.IB_TX_BACK, "tx:menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_text_shapes(current: str) -> InlineKeyboardMarkup:
    import textemoji
    rows, row = [], []
    for key, label in textemoji.SHAPES.items():
        row.append(_btn((T.IB_TX_MARK if key == current else "") + label, f"tx:shape:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([_btn(T.IB_TX_BACK, "tx:menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_text_fx(style) -> InlineKeyboardMarkup:
    import textemoji
    onoff = lambda v: T.ON if v else T.OFF
    return InlineKeyboardMarkup([
        [_btn(T.IB_FX_GLOW.format(v=textemoji.GLOW_NAMES[style.glow]), "tx:glow"),
         _btn(T.IB_FX_COLOR, "tx:menu:gc")],
        [_btn(T.IB_FX_OUTLINE.format(v=textemoji.OUTLINE_NAMES[style.outline]), "tx:outline"),
         _btn(T.IB_FX_COLOR, "tx:menu:oc")],
        [_btn(T.IB_FX_SHADOW.format(v=onoff(style.shadow)), "tx:shadow")],
        [_btn(T.IB_FX_GRAD.format(v=onoff(style.grad)), "tx:grad"),
         _btn(T.IB_FX_COLOR, "tx:menu:g2")],
        [_btn(T.IB_FX_BGGRAD.format(v=onoff(style.bg_grad)), "tx:bgrad"),
         _btn(T.IB_FX_COLOR, "tx:menu:b2")],
        [_btn(T.IB_TX_BACK, "tx:menu:main")],
    ])


def kb_text_anim(style) -> InlineKeyboardMarkup:
    import textemoji
    mark = lambda on, text: (T.IB_TX_MARK + text) if on else text
    items = list(textemoji.ANIMS.items())
    rows = [[_btn(mark(style.anim == "none", items[0][1]), "tx:anim:none")]]
    row = []
    for key, label in items[1:]:
        row.append(_btn(mark(style.anim == key, label), f"tx:anim:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([_btn(mark(style.speed == n, lab), f"tx:speed:{n}") for n, (lab, _) in textemoji.SPEEDS.items()])
    rows.append([_btn(T.IB_TX_BACK, "tx:menu:main")])
    return InlineKeyboardMarkup(rows)


def kb_text_fmt(style, lock: str | None) -> InlineKeyboardMarkup:
    """lock: формат пака (static/vector/video), если он уже задан."""
    row = []
    for key in ("static", "vector", "video"):
        label = T.FMT_LABELS[key]
        if lock and key != lock:
            label = "🔒 " + label
        elif style.fmt == key:
            label = T.IB_TX_MARK + label
        row.append(_btn(label, f"tx:fmt:{key}"))
    rows = [row[:1], row[1:2], row[2:3]]
    if lock:
        rows.append([_btn(T.IB_TX_NEWPACK, "tx:newpack")])
    rows.append([_btn(T.IB_TX_BACK, "tx:menu:main")])
    return InlineKeyboardMarkup(rows)


# ── Дополнительные функции ──

def kb_extra() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T.IB_X_RECOLOR, callback_data="x:recolor")],
        [InlineKeyboardButton(T.IB_X_FILE,    callback_data="x:file")],
        [InlineKeyboardButton(T.IB_X_CLOSE,   callback_data="x:close")],
    ])


def kb_recolor(mode: str) -> InlineKeyboardMarkup:
    def lab(active, text):
        return (T.IB_TX_MARK + text) if active else text
    rows = [[
        InlineKeyboardButton(lab(mode == "mono", T.IB_RC_MONO), callback_data="rc:mode:mono"),
        InlineKeyboardButton(lab(mode == "hue",  T.IB_RC_HUE),  callback_data="rc:mode:hue"),
    ]]
    rows += _palette_rows("rc:c", None)
    rows.append([InlineKeyboardButton(T.IB_TX_HEX, callback_data="rc:hex")])
    rows.append([InlineKeyboardButton(T.IB_CANCEL, callback_data="action:cancel")])
    return InlineKeyboardMarkup(rows)


def kb_cancel_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(T.IB_CANCEL, callback_data="action:cancel")]])


def kb_job_stop() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(T.IB_JOB_STOP, callback_data="job:stop")]])
