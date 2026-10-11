"""
ID премиум-эмодзи и их названия.

Идея из отдельного бота: по сообщению с премиум-эмодзи показать для каждого его id и название.
Название берётся у обычного эмодзи, которым премиум-эмодзи подписан (🙂 -> «Слегка улыбается»).
Библиотека emoji необязательна: без неё названия просто не показываются.
"""
import html

MAX_MSG_LEN = 4096
MAX_LINES = 40            # в одном сообщении не больше стольких премиум-эмодзи: у Telegram есть лимит на сущности


def emoji_name(chars: str) -> str:
    """Русское название эмодзи, запасной вариант английское. Пустая строка, если не нашлось."""
    try:
        import emoji as lib
    except ImportError:
        return ""
    for lang in ("ru", "en"):
        try:
            raw = lib.demojize(chars, language=lang)
        except Exception:
            continue
        if raw.startswith(":") and raw.endswith(":") and raw != chars:
            return raw.strip(":").replace("_", " ").capitalize()
    return ""


def id_line(emoji_id, chars: str) -> str:
    """Строка ответа: сам эмодзи, его ID (по нажатию копируется) и название."""
    name = emoji_name(chars)
    tail = f" {html.escape(name)}" if name else ""
    return f'<emoji id="{emoji_id}">{html.escape(chars)}</emoji> <code>{emoji_id}</code>{tail}'


def split_message(text: str, max_len: int = MAX_MSG_LEN, max_lines: int = MAX_LINES) -> list[str]:
    """Режет ответ по строкам: не длиннее max_len символов и не больше max_lines строк в сообщении."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        extra = len(line) + (1 if current else 0)
        if current and (size + extra > max_len or len(current) >= max_lines):
            parts.append("\n".join(current))
            current, size, extra = [], 0, len(line)
        while len(line) > max_len:                      # одна строка длиннее лимита: режем внутри
            parts.append(line[:max_len])
            line = line[max_len:]
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        parts.append("\n".join(current))
    return parts


def table(rows: list[tuple]) -> str:
    """Текст для файла: номер, эмодзи, ID, название (через табуляцию). rows: [(эмодзи, id)]."""
    out = []
    for i, (chars, emoji_id) in enumerate(rows, 1):
        out.append(f"{i}\t{chars}\t{emoji_id}\t{emoji_name(chars)}")
    return "\n".join(out) + "\n"
