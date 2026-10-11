"""
Разбор сообщений с форматированием: текст или подпись к медиа, премиум-эмодзи, исходный код разметки.

Зачем: премиум-эмодзи чаще всего приходят пересланными постами из каналов, а там они лежат
не в тексте, а в подписи к фото или видео. Поэтому все функции бота читают и текст, и подпись.
Заодно из сущностей Telegram собирается исходный код сообщения, который можно вставить в своего бота:

  * HTML для Bot API (aiogram, python-telegram-bot): <b>, <i>, <a href>, <tg-emoji emoji-id="...">
  * MarkdownV2 для Bot API: *жирный*, _курсив_, [ссылка](url), ![🙂](tg://emoji?id=...)

Смещения сущностей в Telegram считаются в единицах UTF-16, поэтому текст переводится в
"суррогатный" вид (символ вне BMP занимает два знака), и срезы по смещениям совпадают с Telegram.
"""
import html
import re

from hydrogram import enums
from hydrogram.parser.utils import add_surrogates, remove_surrogates

ET = enums.MessageEntityType

# Типы сущностей, которые превращаются в разметку. Остальные (ссылки, упоминания, хэштеги...)
# Telegram находит в обычном тексте сам.
KIND = {
    ET.BOLD: "bold", ET.ITALIC: "italic", ET.UNDERLINE: "underline", ET.STRIKETHROUGH: "strike",
    ET.SPOILER: "spoiler", ET.CODE: "code", ET.PRE: "pre", ET.TEXT_LINK: "link",
    ET.TEXT_MENTION: "mention", ET.BLOCKQUOTE: "blockquote", ET.CUSTOM_EMOJI: "emoji",
}
# При одинаковых границах кто снаружи: чем меньше число, тем ближе к краю.
# Курсив снаружи подчёркивания, иначе в MarkdownV2 закрытие даёт неоднозначные "___".
OUTER = {"link": 0, "mention": 0, "blockquote": 1, "italic": 2, "bold": 3, "strike": 4, "spoiler": 5,
         "underline": 6, "emoji": 8, "code": 9, "pre": 9}


# ════════════════════════════════════════════════════════════════
#  ВХОДНЫЕ СООБЩЕНИЯ
# ════════════════════════════════════════════════════════════════

def body(message) -> tuple[str, list]:
    """(текст, сущности) из текста сообщения или из подписи к медиа."""
    if getattr(message, "text", None):
        return str(message.text), list(message.entities or [])
    if getattr(message, "caption", None):
        return str(message.caption), list(message.caption_entities or [])
    return "", []


def slice16(text: str, offset: int, length: int) -> str:
    """Кусок текста по смещению и длине в единицах UTF-16."""
    return remove_surrogates(add_surrogates(text)[offset: offset + length])


def custom_emojis(message) -> list[tuple]:
    """[(сущность, эмодзи-подпись)] в порядке следования. Читает и текст, и подпись."""
    text, ents = body(message)
    found = [e for e in ents if e.type == ET.CUSTOM_EMOJI and getattr(e, "custom_emoji_id", None)]
    found.sort(key=lambda e: e.offset)
    return [(e, slice16(text, e.offset, e.length)) for e in found]


def has_markup(message) -> bool:
    """Есть ли в сообщении форматирование, которое стоит показать как код."""
    _, ents = body(message)
    return any(e.type in KIND for e in ents)


# ════════════════════════════════════════════════════════════════
#  ФОРМАТЫ
# ════════════════════════════════════════════════════════════════

class Html:
    @staticmethod
    def open(it) -> str:
        kind, e = it[2], it[3]
        if kind == "pre":
            lang = getattr(e, "language", None)
            return f'<pre><code class="language-{html.escape(lang)}">' if lang else "<pre>"
        if kind == "link":
            return f'<a href="{html.escape(e.url or "", quote=True)}">'
        if kind == "mention":
            return f'<a href="tg://user?id={e.user.id}">'
        if kind == "emoji":
            return f'<tg-emoji emoji-id="{e.custom_emoji_id}">'
        return {"bold": "<b>", "italic": "<i>", "underline": "<u>", "strike": "<s>", "spoiler": "<tg-spoiler>",
                "code": "<code>", "blockquote": "<blockquote>"}[kind]

    @staticmethod
    def close(it) -> str:
        kind, e = it[2], it[3]
        if kind == "pre":
            return "</code></pre>" if getattr(e, "language", None) else "</pre>"
        return {"bold": "</b>", "italic": "</i>", "underline": "</u>", "strike": "</s>", "spoiler": "</tg-spoiler>",
                "code": "</code>", "blockquote": "</blockquote>", "link": "</a>", "mention": "</a>",
                "emoji": "</tg-emoji>"}[kind]

    @staticmethod
    def text(seg: str, stack: list) -> str:
        return html.escape(seg, quote=False)


_MD_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_MD_CODE = re.compile(r"([`\\])")
_MD_URL = re.compile(r"([)\\])")


class MarkdownV2:
    @staticmethod
    def open(it) -> str:
        kind, e = it[2], it[3]
        if kind == "pre":
            return f"```{getattr(e, 'language', None) or ''}\n"
        return {"bold": "*", "italic": "_", "underline": "__", "strike": "~", "spoiler": "||", "code": "`",
                "link": "[", "mention": "[", "blockquote": ">", "emoji": "!["}[kind]

    @staticmethod
    def close(it) -> str:
        kind, e = it[2], it[3]
        if kind == "link":
            url = _MD_URL.sub(r"\\\1", e.url or "")
            return f"]({url})"
        if kind == "mention":
            return f"](tg://user?id={e.user.id})"
        if kind == "emoji":
            return f"](tg://emoji?id={e.custom_emoji_id})"
        if kind == "pre":
            return "\n```"
        return {"bold": "*", "italic": "_", "underline": "__", "strike": "~", "spoiler": "||", "code": "`",
                "blockquote": ""}[kind]

    @staticmethod
    def text(seg: str, stack: list) -> str:
        kinds = {it[2] for it in stack}
        if kinds & {"code", "pre"}:
            return _MD_CODE.sub(r"\\\1", seg)
        seg = _MD_SPECIAL.sub(r"\\\1", seg)
        if "blockquote" in kinds:
            seg = seg.replace("\n", "\n>")
        return seg


def _render(text: str, entities: list, fmt) -> str:
    u = add_surrogates(text)
    n = len(u)
    items = []
    for i, e in enumerate(entities or []):
        kind = KIND.get(e.type)
        if kind is None or e.length <= 0:
            continue
        if kind == "mention" and not getattr(e, "user", None):
            continue
        if kind == "link" and not getattr(e, "url", None):
            continue
        start, end = max(0, e.offset), min(n, e.offset + e.length)
        if start < end:
            items.append((start, end, kind, e, i))
    # снаружи тот, кто начался раньше или длиннее; при равенстве решает OUTER
    items.sort(key=lambda it: (it[0], -it[1], OUTER.get(it[2], 7), it[4]))

    points = sorted({0, n} | {it[0] for it in items} | {it[1] for it in items})
    out: list[str] = []
    stack: list = []
    for a, b in zip(points, points[1:]):
        active = [it for it in items if it[0] <= a and it[1] >= b]
        k = 0
        while k < len(stack) and k < len(active) and stack[k] is active[k]:
            k += 1
        for it in reversed(stack[k:]):
            out.append(fmt.close(it))
        for it in active[k:]:
            out.append(fmt.open(it))
        stack = active
        out.append(fmt.text(u[a:b], stack))
    for it in reversed(stack):
        out.append(fmt.close(it))
    return remove_surrogates("".join(out))


def to_html(text: str, entities: list) -> str:
    return _render(text, entities, Html)


def to_markdown(text: str, entities: list) -> str:
    return _render(text, entities, MarkdownV2)
