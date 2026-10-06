"""
Режим «Текстовые эмодзи».

Пользователь присылает текст, бот показывает превью с кнопками. Любая правка (цвет, шрифт,
форма, эффекты, анимация, формат) перерисовывает превью в том же сообщении. Для анимированных
стилей превью - зацикленный ролик, для остальных - картинка.
"""
import asyncio
import dataclasses
import logging
import os
import shutil
import tempfile
import uuid

from hydrogram import enums, filters
from hydrogram.types import CallbackQuery, InputMediaAnimation, InputMediaPhoto, Message

import keyboards
import packs
import states
import textemoji
import textlottie
import texts as T
import vector
from tg import app, bot_suffix, h, pack_link

log = logging.getLogger("fatheremoji")
HTML = enums.ParseMode.HTML

_FIELDS = [f for f in dataclasses.fields(textemoji.Style) if f.name != "text"]
DEFAULTS = {f.name: f.default for f in _FIELDS}

# Экран выбора цвета -> поле стиля
COLOR_FIELDS = {"bg": "bg", "fg": "fg", "gc": "glow_color", "oc": "outline_color",
                "g2": "grad_color", "b2": "bg_color2"}
COLOR_TITLES = {"bg": T.TXT_SUB_BG, "fg": T.TXT_SUB_FG, "gc": T.TXT_SUB_GC,
                "oc": T.TXT_SUB_OC, "g2": T.TXT_SUB_G2, "b2": T.TXT_SUB_B2}

_locks: dict[int, asyncio.Lock] = {}


def _user_lock(uid: int) -> asyncio.Lock:
    return _locks.setdefault(uid, asyncio.Lock())


# ════════════════════════════════════════════════════════════════
#  СТИЛЬ И ФОРМАТ ПАКА
# ════════════════════════════════════════════════════════════════

def _lock(data: dict) -> str | None:
    """Формат пака (static/vector/video), если в нём уже есть эмодзи."""
    return textemoji.TG_TO_FMT.get(data.get("pack_fmt"))


def _style(uid: int) -> textemoji.Style:
    data = states.user_states.setdefault(uid, {})
    if "txt" not in data:
        data["txt"] = {**DEFAULTS, **states.text_prefs.get(uid, {}), "text": ""}
    keep = {k: v for k, v in data["txt"].items() if k in DEFAULTS or k == "text"}
    st = textemoji.Style(**{**DEFAULTS, **keep})
    lock = _lock(data)
    if lock:
        st.fmt = lock
    return st


def _save(uid: int, st: textemoji.Style):
    data = states.user_states[uid]
    data["txt"] = st.__dict__.copy()
    prefs = {f.name: getattr(st, f.name) for f in _FIELDS}
    if _lock(data):                    # формат пака не должен становиться привычкой пользователя
        prefs["fmt"] = states.text_prefs.get(uid, {}).get("fmt", DEFAULTS["fmt"])
    states.text_prefs[uid] = prefs


def _apply_color(st: textemoji.Style, which: str, value: str | None):
    """Ставит цвет и, где нужно, включает связанный эффект (выбрал цвет свечения: свечение включилось)."""
    field = COLOR_FIELDS[which]
    if value is None:
        setattr(st, field, None)
        if which == "bg":
            st.shape = "none"
        return
    setattr(st, field, value)
    if which == "bg" and st.shape == "none":
        st.shape = "round"
    elif which == "gc" and not st.glow:
        st.glow = 2
    elif which == "oc" and not st.outline:
        st.outline = 2
    elif which == "g2":
        st.grad = True
    elif which == "b2":
        st.bg_grad = True
        if st.shape == "none":
            st.shape = "round"
        if st.bg is None:
            st.bg = DEFAULTS["bg"]


# ════════════════════════════════════════════════════════════════
#  ТЕКСТЫ ЭКРАНОВ
# ════════════════════════════════════════════════════════════════

def _fx_line(st: textemoji.Style) -> str:
    fx = []
    if st.glow:
        fx.append(f"свечение ({textemoji.GLOW_NAMES[st.glow]})")
    if st.outline:
        fx.append(f"обводка ({textemoji.OUTLINE_NAMES[st.outline]})")
    if st.shadow:
        fx.append("тень")
    if st.grad:
        fx.append("градиент текста")
    if st.bg_grad and st.shape != "none":
        fx.append("градиент фона")
    return ", ".join(fx) or T.TXT_NONE


def _anim_line(st: textemoji.Style) -> str:
    if st.anim == "none":
        return T.TXT_NONE
    name = textemoji.ANIMS[st.anim]
    if st.fmt == "static":
        return T.TXT_ANIM_OFF_STATIC.format(name=name)
    speed = textemoji.SPEEDS[st.speed][0].split(" ", 1)[1].lower()
    return T.TXT_ANIM_LINE.format(name=name, speed=speed)


def _fmt_label(st: textemoji.Style, lock: str | None) -> str:
    return T.FMT_SHORT[st.fmt] + (" 🔒" if lock else "")


def _caption(st: textemoji.Style, data: dict) -> str:
    lock = _lock(data)
    warn = ""
    if data.pop("txt_cut", False):
        warn += T.TXT_WARN_CUT.format(max=textemoji.MAX_CHARS)
    miss = textemoji.missing_chars(st.text, st.font)
    if miss:
        warn += T.TXT_WARN_MISSING.format(chars=h(miss))
    if textemoji.low_contrast(st):
        warn += T.TXT_WARN_CONTRAST
    if lock in ("vector", "video"):
        warn += T.TXT_WARN_LOCK.format(fmt=T.FMT_PACK[lock])
    fmt = T.FMT_SHORT[st.fmt] + (" (как в паке)" if lock else "")
    return T.TXT_PANEL.format(
        text=h(st.text),
        bg=keyboards.color_name(st.bg if st.shape != "none" else None),
        fg=keyboards.color_name(st.fg),
        font=textemoji.FONTS[st.font][1],
        shape=textemoji.SHAPES[st.shape].lower(),
        fx=_fx_line(st),
        anim=_anim_line(st),
        fmt=fmt,
        warn=warn,
    )


def _view(st: textemoji.Style, data: dict, view: str):
    """-> (подпись, клавиатура) для экрана панели."""
    lock = _lock(data)
    if view == "main":
        return _caption(st, data), keyboards.kb_text_main(_fmt_label(st, lock))
    if view in COLOR_FIELDS:
        cur = getattr(st, COLOR_FIELDS[view])
        if view == "bg" and st.shape == "none":
            cur = None
        return COLOR_TITLES[view], keyboards.kb_text_colors(view, cur)
    if view == "font":
        return T.TXT_SUB_FONT, keyboards.kb_text_fonts(st.font)
    if view == "shape":
        return T.TXT_SUB_SHAPE, keyboards.kb_text_shapes(st.shape)
    if view == "fx":
        return T.TXT_SUB_FX, keyboards.kb_text_fx(st)
    if view == "anim":
        note = T.TXT_NOTE_ANIM_LOCKED if lock == "static" else (T.TXT_NOTE_ANIM_STATIC if st.fmt == "static" else "")
        return T.TXT_SUB_ANIM.format(note=note), keyboards.kb_text_anim(st)
    if view == "fmt":
        lock_text = T.TXT_FMT_LOCK.format(fmt=T.FMT_PACK[lock]) if lock else T.TXT_FMT_FREE
        return T.TXT_SUB_FMT.format(lock=lock_text), keyboards.kb_text_fmt(st, lock)
    return _caption(st, data), keyboards.kb_text_main(_fmt_label(st, lock))


# ════════════════════════════════════════════════════════════════
#  ПРЕВЬЮ И ОТПРАВКА ПАНЕЛИ
# ════════════════════════════════════════════════════════════════

def _preview_sync(st: textemoji.Style):
    """-> (BytesIO, "photo" | "anim"). Анимированные стили дают зацикленный mp4."""
    if textemoji.animated(st):
        tmp = tempfile.mkdtemp()
        try:
            tgs = None
            if st.fmt == "vector":
                try:
                    tgs = os.path.join(tmp, "p.tgs")
                    textlottie.build_tgs(st, tgs)
                except vector.ImageTooComplex:
                    tgs = None
            buf = textemoji.render_preview_anim(st, tgs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if buf:
            return buf, "anim"
    return textemoji.render_preview_png(st), "photo"


async def _preview(st: textemoji.Style):
    return await asyncio.to_thread(_preview_sync, st)


async def _send_new(client, chat_id: int, buf, kind: str, cap: str, kb):
    if kind == "anim":
        return await client.send_animation(chat_id, buf, caption=cap, reply_markup=kb, parse_mode=HTML,
                                           width=360, height=360, duration=3)
    return await client.send_photo(chat_id, buf, caption=cap, reply_markup=kb, parse_mode=HTML)


async def _strip_old_panel(client, chat_id: int, data: dict):
    old = data.pop("panel_id", None)
    if old:
        try:
            await client.edit_message_reply_markup(chat_id, old, reply_markup=None)
        except Exception:
            pass


async def send_panel(client, message: Message, uid: int, view: str = "main"):
    """Новое сообщение-панель (старую панель отключаем, чтобы не путать кнопки)."""
    data = states.user_states[uid]
    st = _style(uid)
    chat_id = message.chat.id
    await _strip_old_panel(client, chat_id, data)
    try:
        await client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_VIDEO if textemoji.animated(st) else enums.ChatAction.UPLOAD_PHOTO)
    except Exception:
        pass
    buf, kind = await _preview(st)
    cap, kb = _view(st, data, view)
    sent = await _send_new(client, chat_id, buf, kind, cap, kb)
    data.update({"panel_id": sent.id, "panel_kind": kind, "state": "TXT_PANEL"})


async def _redraw(client, cb: CallbackQuery, uid: int, view: str):
    """Перерисовать превью и показать нужный экран. Тип превью поменялся (картинка/ролик): шлём новое сообщение."""
    data = states.user_states[uid]
    st = _style(uid)
    buf, kind = await _preview(st)
    cap, kb = _view(st, data, view)

    if kind == data.get("panel_kind"):
        media = (InputMediaAnimation(buf, caption=cap, parse_mode=HTML, width=360, height=360, duration=3)
                 if kind == "anim" else InputMediaPhoto(buf, caption=cap, parse_mode=HTML))
        try:
            await cb.message.edit_media(media, reply_markup=kb)
            return
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" in str(e).upper():
                return
            log.warning(f"edit_media не прошёл, шлю панель заново: {e}")
            buf.seek(0)

    chat_id = cb.message.chat.id
    sent = await _send_new(client, chat_id, buf, kind, cap, kb)
    try:
        await cb.message.delete()
    except Exception:
        pass
    data.update({"panel_id": sent.id, "panel_kind": kind})


async def _show_view(cb: CallbackQuery, uid: int, view: str):
    """Смена экрана без перерисовки картинки."""
    data = states.user_states[uid]
    cap, kb = _view(_style(uid), data, view)
    try:
        await cb.message.edit_caption(cap, reply_markup=kb, parse_mode=HTML)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            raise


# ════════════════════════════════════════════════════════════════
#  ВХОД В РЕЖИМ И ТЕКСТОВЫЕ СООБЩЕНИЯ
# ════════════════════════════════════════════════════════════════

async def start(client, cb: CallbackQuery, uid: int):
    """Нажата кнопка «Текстовые эмодзи»: пак уже выбран или создан, ждём текст."""
    data = states.user_states.setdefault(uid, {})
    data.pop("txt", None)
    data["pack_fmt"] = await packs.dest_format(data["link"])
    data["state"] = "TXT_WAIT"
    await cb.message.edit_text(T.TXT_ASK.format(max=textemoji.MAX_CHARS), parse_mode=HTML)
    await cb.message.reply(T.SESSION_ADD_HINT, reply_markup=keyboards.SESSION_KB)


async def on_text(client, message: Message, uid: int, state: str):
    data = states.user_states[uid]
    raw = message.text.strip()

    if state == "TXT_HEX":
        hx = textemoji.parse_hex(raw)
        if not hx:
            return await message.reply(T.TXT_HEX_BAD, parse_mode=HTML)
        st = _style(uid)
        _apply_color(st, data.pop("hex_for", "fg"), hx)
        _save(uid, st)
        return await send_panel(client, message, uid)

    text = textemoji.clean_text(raw)
    if not text:
        return await message.reply(T.TXT_EMPTY)
    if len(" ".join(raw.split())) > textemoji.MAX_CHARS:
        data["txt_cut"] = True
    st = _style(uid)
    st.text = text
    _save(uid, st)
    await send_panel(client, message, uid)


# ════════════════════════════════════════════════════════════════
#  КНОПКИ ПАНЕЛИ
# ════════════════════════════════════════════════════════════════

def _live(uid: int) -> bool:
    d = states.user_states.get(uid, {})
    return d.get("state", "").startswith("TXT_") and bool(d.get("txt", {}).get("text"))


@app.on_callback_query(filters.regex(r"^tx:"))
async def cb_text(client, cb: CallbackQuery):
    uid = cb.from_user.id
    if not _live(uid):
        return await cb.answer(T.TXT_STALE, show_alert=True)
    async with _user_lock(uid):
        await _handle(client, cb, uid)


async def _handle(client, cb: CallbackQuery, uid: int):
    data = states.user_states[uid]
    parts = cb.data.split(":")
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None
    st = _style(uid)
    lock = _lock(data)

    # ── переходы между экранами ──
    if action == "menu":
        await cb.answer()
        return await _show_view(cb, uid, arg)

    if action == "hex":
        await cb.answer()
        data["state"] = "TXT_HEX"
        data["hex_for"] = arg
        return await cb.message.reply(T.TXT_HEX_ASK, parse_mode=HTML)

    if action == "add":
        return await _add(client, cb, uid, st, data)

    if action == "newpack":
        return await _new_pack(client, cb, uid, st, data)

    # ── изменения стиля ──
    toast = None
    view = "main"

    if action == "col":
        which, val = arg, parts[3]
        _apply_color(st, which, None if val == "none" else "#" + val)
        view = which
    elif action == "font" and arg in textemoji.FONTS:
        st.font, view = arg, "font"
    elif action == "shape" and arg in textemoji.SHAPES:
        st.shape, view = arg, "shape"
        if arg == "none":
            st.bg = None
        elif st.bg is None:
            st.bg = DEFAULTS["bg"]
    elif action == "glow":
        st.glow, view = (st.glow + 1) % len(textemoji.GLOW_NAMES), "fx"
    elif action == "outline":
        st.outline, view = (st.outline + 1) % len(textemoji.OUTLINE_NAMES), "fx"
    elif action == "shadow":
        st.shadow, view = not st.shadow, "fx"
    elif action == "grad":
        st.grad, view = not st.grad, "fx"
    elif action == "bgrad":
        st.bg_grad, view = not st.bg_grad, "fx"
        if st.bg_grad and st.shape == "none":
            st.shape = "round"
            st.bg = st.bg or DEFAULTS["bg"]
    elif action == "speed" and arg is not None:
        st.speed, view = int(arg), "anim"
    elif action == "anim" and arg in textemoji.ANIMS:
        view = "anim"
        if arg != "none":
            if lock == "static":
                return await cb.answer(T.TXT_TOAST_ANIM_LOCKED, show_alert=True)
            if st.fmt == "static":
                st.fmt = "vector"
                toast = T.TXT_TOAST_TO_VECTOR
            if arg == "glow" and not st.glow:
                st.glow = 2
        st.anim = arg
    elif action == "fmt" and arg in textemoji.FMT_TO_TG:
        if lock and arg != lock:
            return await cb.answer(T.TXT_TOAST_LOCKED, show_alert=True)
        st.fmt, view = arg, "fmt"
    else:
        return await cb.answer()

    _save(uid, st)
    await cb.answer(toast or (T.TXT_RENDER if textemoji.animated(_style(uid)) else None))
    await _redraw(client, cb, uid, view)


async def _new_pack(client, cb: CallbackQuery, uid: int, st: textemoji.Style, data: dict):
    """Отдельный пак под другой формат: формат пака задаётся первым эмодзи и потом не меняется."""
    old_title = data.get("title", "Emoji")
    data["link"] = f"txt{uuid.uuid4().hex[:8]}{bot_suffix()}"
    data["title"] = f"{old_title[:50]} (текст)"
    data["pack_fmt"] = None
    st.fmt = "static"                                   # новый пак всегда начинается со статики
    _save(uid, st)
    await cb.answer()
    await cb.message.reply(T.TXT_NEWPACK.format(title=h(data["title"])), parse_mode=HTML)
    await _redraw(client, cb, uid, "fmt")


async def _add(client, cb: CallbackQuery, uid: int, st: textemoji.Style, data: dict):
    link = data.get("link")
    if not link:
        return await cb.answer(T.PACK_GONE, show_alert=True)
    await cb.answer(T.TXT_BUILDING)

    res = await packs.add_text_emoji(uid, link, data.get("title", "My Emoji Pack"), st,
                                     data.get("default_emoji", "✨"))
    if not res["ok"]:
        return await cb.message.reply(T.TXT_ERR.format(desc=res["err"]), parse_mode=HTML)

    data["pack_fmt"] = await packs.dest_format(link)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    data.pop("panel_id", None)
    data["state"] = "TXT_WAIT"

    emoji = data.get("default_emoji", "✨")
    shown = f'<emoji id="{res["cid"]}">{emoji}</emoji>' if res["cid"] else emoji
    note = T.TXT_ADDED_NOTE.format(items=", ".join(res["notes"])) if res["notes"] else ""
    await cb.message.reply(
        T.TXT_ADDED.format(emoji=shown, fmt=T.FMT_SHORT[res["fmt"]], note=note, link=pack_link(link)),
        disable_web_page_preview=True, parse_mode=HTML)
