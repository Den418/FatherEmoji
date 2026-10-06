"""
Текстовые эмодзи: слово или фраза на цветном фоне, с эффектами и анимацией.

Форматы результата:
  static - webp 100x100 (рисует Pillow)
  vector - .tgs, контуры букв берутся из шрифта (textlottie.py)
  video  - webm 100x100, кадры рисует Pillow (настоящее размытие, любые эффекты)

Раскладка текста, формулы анимаций и размеры эффектов общие для всех форматов,
поэтому один и тот же стиль выглядит почти одинаково везде.
"""
import colorsys
import io
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

import numpy as np
from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# key -> (файл, подпись в меню)
FONTS = {
    "mont":   ("Montserrat-ExtraBold.ttf",        "Montserrat"),
    "fira":   ("FiraSansCondensed-ExtraBold.ttf", "Fira Condensed"),
    "oswald": ("Oswald-Bold.ttf",                 "Oswald"),
    "russo":  ("RussoOne-Regular.ttf",            "Russo One"),
    "unb":    ("Unbounded-Bold.ttf",              "Unbounded"),
    "comf":   ("Comfortaa-Bold.ttf",              "Comfortaa"),
    "play":   ("PlayfairDisplay-Bold.ttf",        "Playfair"),
    "caveat": ("Caveat-Bold.ttf",                 "Caveat"),
    "amatic": ("AmaticSC-Bold.ttf",               "Amatic"),
    "marck":  ("MarckScript-Regular.ttf",         "Marck Script"),
    "pacif":  ("Pacifico-Regular.ttf",            "Pacifico"),
    "lobst":  ("Lobster-Regular.ttf",             "Lobster"),
    "pixel":  ("PressStart2P-Regular.ttf",        "Pixel"),
}
DEFAULT_FONT = "mont"

SHAPES = {"round": "Скруглённый", "circle": "Круг", "square": "Квадрат", "none": "Без фона"}

# Формат эмодзи в терминах бота -> формат в терминах Telegram и обратно
FMT_TO_TG = {"static": "static", "vector": "animated", "video": "video"}
TG_TO_FMT = {v: k for k, v in FMT_TO_TG.items()}

GLOW_NAMES = ["выкл", "слабое", "среднее", "сильное"]
OUTLINE_NAMES = ["выкл", "тонкая", "средняя", "толстая"]

# Анимации: ключ -> подпись. Порядок задаёт порядок кнопок.
ANIMS = {
    "none":    "⏹ Без анимации",
    "pulse":   "💓 Пульс",
    "wobble":  "🎯 Качание",
    "bounce":  "🏀 Прыжок",
    "shake":   "📳 Дрожь",
    "blink":   "💫 Мигание",
    "glow":    "🔆 Пульс свечения",
    "rainbow": "🌈 Радуга",
    "wave":    "🌊 Волна",
    "typing":  "⌨️ Печать",
}
PER_GLYPH = {"wave", "typing"}                 # эти анимации двигают каждую букву отдельно

# Скорость: подпись и число кадров в цикле при 30 кадрах/с (Telegram разрешает до 3 секунд)
SPEEDS = {0: ("🐢 Медленно", 84), 1: ("⏱ Обычно", 60), 2: ("🐇 Быстро", 36)}

MAX_CHARS = 60
MAX_LINES = 5
SIZE = 512                                      # логический холст (как у .tgs)

# Размеры эффектов в логических пикселях холста 512
OUTLINE_W = {1: 5, 2: 9, 3: 14}                 # видимая толщина обводки
GLOW_LEVELS = {                                 # r: радиус, n и op: слои и прозрачность (вектор), gain: усиление (растр)
    1: dict(r=10, n=4, op=0.15, gain=0.80, spread=1),
    2: dict(r=16, n=5, op=0.18, gain=0.95, spread=2),
    3: dict(r=24, n=6, op=0.19, gain=1.00, spread=3),
}
SHADOW_OFFSET = (4.0, 6.0)


@dataclass
class Style:
    text: str = ""
    bg: str | None = "#2F7BFF"           # None = без фона
    fg: str = "#FFFFFF"
    font: str = DEFAULT_FONT
    shape: str = "round"
    fmt: str = "static"                  # static | vector | video
    glow: int = 0                        # 0..3
    glow_color: str | None = None        # None = цвет текста
    outline: int = 0                     # 0..3
    outline_color: str | None = None     # None = автоматически контрастный
    shadow: bool = False
    grad: bool = False                   # градиент текста
    grad_color: str = "#FF5FA2"
    bg_grad: bool = False                # градиент фона
    bg_color2: str = "#8E44E8"
    anim: str = "none"
    speed: int = 1

    def copy(self):
        return Style(**self.__dict__)


# ════════════════════════════════════════════════════════════════
#  ЦВЕТ
# ════════════════════════════════════════════════════════════════

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_hex(raw: str) -> str | None:
    m = _HEX.match((raw or "").strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(hex_color: str) -> float:
    r, g, b = to_rgb(hex_color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def low_contrast(style: Style) -> bool:
    """Текст почти не виден на фоне: стоит предупредить пользователя."""
    if style.bg is None or style.shape == "none":
        return False
    return abs(luminance(style.bg) - luminance(style.fg)) < 0.18 and not (style.outline or style.glow)


def auto_outline(style: Style) -> str:
    return style.outline_color or ("#1C1C1E" if luminance(style.fg) > 0.55 else "#FFFFFF")


def rainbow_base(fg: str) -> tuple[float, float, float]:
    """Исходные (h, s, v) для радуги. Белый и серый текст получают насыщенность, иначе радуги не видно."""
    r, g, b = (c / 255 for c in to_rgb(fg))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.35:
        s, v = 0.85, 1.0
    return h, s, max(v, 0.85)


def hue_color(fg: str, shift: float) -> tuple[int, int, int]:
    h, s, v = rainbow_base(fg)
    r, g, b = colorsys.hsv_to_rgb((h + shift) % 1.0, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def text_rgb(style: Style, hue: float | None = None) -> tuple[int, int, int]:
    return hue_color(style.fg, hue) if hue is not None else to_rgb(style.fg)


def glow_rgb(style: Style, hue: float | None = None) -> tuple[int, int, int]:
    return to_rgb(style.glow_color) if style.glow_color else text_rgb(style, hue)


# ════════════════════════════════════════════════════════════════
#  ШРИФТЫ
# ════════════════════════════════════════════════════════════════

_font_cache: dict[str, TTFont] = {}


def _tt(font_key: str) -> TTFont:
    if font_key not in _font_cache:
        fname = FONTS.get(font_key, FONTS[DEFAULT_FONT])[0]
        _font_cache[font_key] = TTFont(os.path.join(FONT_DIR, fname))
    return _font_cache[font_key]


class _Collector(BasePen):
    """Собирает контуры глифа в формате Lottie (вершины и касательные)."""

    def __init__(self, glyph_set):
        super().__init__(glyph_set)
        self.contours = []
        self._cur = None

    def _moveTo(self, pt):
        self._cur = {"v": [list(pt)], "i": [[0, 0]], "o": [[0, 0]]}

    def _lineTo(self, pt):
        self._cur["v"].append(list(pt)); self._cur["i"].append([0, 0]); self._cur["o"].append([0, 0])

    def _curveToOne(self, p1, p2, p3):
        c = self._cur
        px, py = c["v"][-1]
        c["o"][-1] = [p1[0] - px, p1[1] - py]
        c["v"].append(list(p3))
        c["i"].append([p2[0] - p3[0], p2[1] - p3[1]])
        c["o"].append([0, 0])

    def _closePath(self):
        c = self._cur
        if c and len(c["v"]) > 1:
            if abs(c["v"][-1][0] - c["v"][0][0]) < 0.01 and abs(c["v"][-1][1] - c["v"][0][1]) < 0.01:
                c["i"][0] = c["i"][-1]
                c["v"].pop(); c["i"].pop(); c["o"].pop()
            self.contours.append(c)
        self._cur = None

    def _endPath(self):
        self._cur = None


class FontMetrics:
    def __init__(self, key: str):
        self.tt = _tt(key)
        self.upm = self.tt["head"].unitsPerEm
        self.cmap = self.tt.getBestCmap()
        self.gs = self.tt.getGlyphSet()
        self.hmtx = self.tt["hmtx"]
        self._bounds = {}

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def glyph(self, ch: str) -> str:
        return self.cmap.get(ord(ch)) or self.cmap.get(ord("?")) or ".notdef"

    def adv(self, ch: str) -> float:
        return self.hmtx[self.glyph(ch)][0] / self.upm

    def width(self, line: str) -> float:
        return sum(self.adv(c) for c in line)

    def ink(self, ch: str):
        """(xMin, yMin, xMax, yMax) в em или None для пробела."""
        g = self.glyph(ch)
        if g not in self._bounds:
            bp = BoundsPen(self.gs)
            self.gs[g].draw(bp)
            b = bp.bounds
            self._bounds[g] = None if b is None else tuple(v / self.upm for v in b)
        return self._bounds[g]


# ════════════════════════════════════════════════════════════════
#  АНИМАЦИЯ: позы
# ════════════════════════════════════════════════════════════════

@dataclass
class Pose:
    scale: float = 1.0
    rot: float = 0.0          # градусы, по часовой
    dx: float = 0.0
    dy: float = 0.0
    alpha: float = 1.0
    glow_k: float = 1.0
    hue: float | None = None  # сдвиг оттенка для радуги


def animated(style: Style) -> bool:
    """Есть ли у результата движение (статичный формат анимацию не умеет)."""
    return style.fmt != "static" and style.anim != "none"


def loop_frames(style: Style) -> int:
    return SPEEDS.get(style.speed, SPEEDS[1])[1] if animated(style) else 1


def _cyc(t: float) -> float:
    """0 -> 1 -> 0 за один цикл."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * t)


def pose_at(style: Style, t: float) -> Pose:
    a = style.anim
    p = Pose()
    if a == "pulse":
        p.scale = 1 + 0.10 * _cyc(t)
    elif a == "wobble":
        p.rot = 8 * math.sin(2 * math.pi * t)
    elif a == "bounce":
        p.dy = -30 * abs(math.sin(2 * math.pi * t))
    elif a == "shake":
        p.dx = 7 * math.sin(2 * math.pi * 6 * t)
        p.rot = 2.5 * math.sin(2 * math.pi * 6 * t + 0.8)
    elif a == "blink":
        p.alpha = 0.25 + 0.75 * (0.5 + 0.5 * math.cos(2 * math.pi * t))
    elif a == "glow":
        p.glow_k = 0.2 + 0.8 * _cyc(t)
    elif a == "rainbow":
        p.hue = t
    return p


def glyph_pose(style: Style, t: float, i: int, n: int) -> tuple[float, float]:
    """(смещение по вертикали, прозрачность) для i-й буквы из n (волна и печать)."""
    if style.anim == "wave":
        return -20 * math.sin(2 * math.pi * (t - 0.9 * i / max(n, 1))), 1.0
    if style.anim == "typing":
        # Нулевой кадр показывает весь текст (это превью в списке эмодзи), потом буквы стираются и печатаются заново.
        if t < TYPING_RESET:
            return 0.0, 1.0
        return 0.0, min(1.0, max(0.0, (t - typing_start(i, n)) / 0.04))
    return 0.0, 1.0


TYPING_RESET = 0.16


def typing_start(i: int, n: int) -> float:
    """Момент цикла, когда появляется i-я буква из n."""
    return TYPING_RESET + 0.04 + 0.58 * i / max(n, 1)


def shrink_factor(style: Style) -> float:
    """Запас по размеру, чтобы анимация и эффекты не вылезали за холст."""
    k = 1.0
    if style.fmt != "static":
        k *= {"pulse": 0.92, "wobble": 0.93, "bounce": 0.90, "shake": 0.95, "wave": 0.90}.get(style.anim, 1.0)
    k *= 1 - 0.035 * style.glow
    k *= 1 - 0.03 * style.outline
    return k


# ════════════════════════════════════════════════════════════════
#  РАСКЛАДКА
# ════════════════════════════════════════════════════════════════

@dataclass
class Layout:
    size_px: float                                    # кегль в пикселях холста 512
    lines: list = field(default_factory=list)          # [(строка, x_левый, y_базовой_линии)]
    box: tuple = (0.0, 0.0, 0.0, 0.0)                  # границы чернил: x0, y0, x1, y1


def _wrap(words: list[str], fm: FontMetrics, size: float, max_w: float) -> list[str]:
    lines, cur = [], ""
    for w in words:
        # слишком длинное слово режем по символам
        while fm.width(w) * size > max_w and len(w) > 1:
            k = len(w)
            while k > 1 and fm.width(w[:k]) * size > max_w:
                k -= 1
            if cur:
                lines.append(cur); cur = ""
            lines.append(w[:k]); w = w[k:]
        trial = (cur + " " + w) if cur else w
        if cur and fm.width(trial) * size > max_w:
            lines.append(cur); cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def layout_text(text: str, font_key: str, shape: str, shrink: float = 1.0) -> Layout:
    fm = FontMetrics(font_key)
    text = " ".join(text.split())[:MAX_CHARS]
    words = text.split(" ") if text else [""]

    pad = {"round": 0.11, "square": 0.09, "circle": 0.19, "none": 0.02}.get(shape, 0.11) * SIZE
    box = (SIZE - 2 * pad) * shrink
    lead = 1.04                                        # межстрочный интервал в em

    def block_height(lines, size):
        # реальная высота чернил: верх первой строки .. низ последней
        tops = [max((fm.ink(c) or (0, 0, 0, 0))[3] for c in ln) for ln in lines]
        bots = [min((fm.ink(c) or (0, 0, 0, 0))[1] for c in ln) for ln in lines]
        step = lead * size
        return (tops[0] * size) + step * (len(lines) - 1) + (-bots[-1] * size), tops, bots

    # Самое длинное слово должно помещаться целиком: рвать слова посередине некрасиво.
    longest = max((fm.width(w) for w in words), default=0.1) or 0.1
    cap_by_word = box / longest
    readable = box * 0.20                       # меньше этого кегля слово разрешаем переносить
    hard_wrap = cap_by_word < readable
    hi0 = box * 1.6 if hard_wrap else min(box * 1.6, cap_by_word)

    best = None
    lo, hi = 8.0, hi0
    for _ in range(26):                         # двоичный поиск максимального кегля
        mid = (lo + hi) / 2
        lines = _wrap(words, fm, mid, box)
        ok = len(lines) <= MAX_LINES
        if ok:
            h, _, _ = block_height(lines, mid)
            ok = h <= box and max(fm.width(ln) for ln in lines) * mid <= box + 0.5
        if ok:
            best, lo = (mid, lines), mid
        else:
            hi = mid
    if best is None:
        size = 8.0
        best = (size, _wrap(words, fm, size, box)[:MAX_LINES])
    size, lines = best

    h, tops, bots = block_height(lines, size)
    y_top = (SIZE - h) / 2
    out = []
    base0 = y_top + tops[0] * size
    xs0, xs1 = [], []
    for i, ln in enumerate(lines):
        w = fm.width(ln) * size
        # визуальное центрирование по чернилам первой и последней букв
        first, last = fm.ink(ln[0]), fm.ink(ln[-1])
        lsb = (first[0] if first else 0) * size
        rsb = (fm.adv(ln[-1]) - (last[2] if last else fm.adv(ln[-1]))) * size
        x = (SIZE - (w - lsb - rsb)) / 2 - lsb
        out.append((ln, x, base0 + i * lead * size))
        xs0.append(x + lsb)
        xs1.append(x + w - rsb)
    return Layout(size_px=size, lines=out, box=(min(xs0), y_top, max(xs1), y_top + h))


def glyph_list(lay: Layout, font_key: str) -> list[tuple[str, float, float]]:
    """Все буквы (без пробелов) с координатами: [(символ, x, базовая линия)] в порядке чтения."""
    fm = FontMetrics(font_key)
    out = []
    for ln, x0, base in lay.lines:
        x = x0
        for ch in ln:
            if ch != " ":
                # символа нет в шрифте: везде рисуем "?" (так же, как делает вектор)
                out.append((ch if fm.has(ch) else "?", x, base))
            x += fm.adv(ch) * lay.size_px
    return out


# ════════════════════════════════════════════════════════════════
#  РЕНДЕР КАДРОВ (Pillow)
# ════════════════════════════════════════════════════════════════

_pil_fonts: dict[tuple, ImageFont.FreeTypeFont] = {}


def _pil_font(key: str, px: float) -> ImageFont.FreeTypeFont:
    k = (key, round(px * 4))
    if k not in _pil_fonts:
        _pil_fonts[k] = ImageFont.truetype(os.path.join(FONT_DIR, FONTS[key][0]), max(1, round(px)))
    return _pil_fonts[k]


def _shape_mask(shape: str, s: int) -> Image.Image:
    m = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(m)
    if shape == "circle":
        d.ellipse((0, 0, s - 1, s - 1), fill=255)
    elif shape == "square":
        d.rectangle((0, 0, s, s), fill=255)
    else:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.24), fill=255)
    return m


def _gradient(px: int, c1, c2, p0, p1) -> Image.Image:
    """Линейный градиент c1 -> c2 от точки p0 к точке p1 (в логических координатах 512)."""
    k = px / SIZE
    x0, y0, x1, y1 = p0[0] * k, p0[1] * k, p1[0] * k, p1[1] * k
    yy, xx = np.mgrid[0:px, 0:px].astype(np.float32)
    vx, vy = x1 - x0, y1 - y0
    den = vx * vx + vy * vy or 1.0
    t = np.clip(((xx - x0) * vx + (yy - y0) * vy) / den, 0, 1)[..., None]
    arr = np.array(c1, np.float32) * (1 - t) + np.array(c2, np.float32) * t
    return Image.fromarray(np.clip(arr + 0.5, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def render_bg(style: Style, px: int) -> Image.Image:
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    if style.bg and style.shape != "none":
        if style.bg_grad:
            layer = _gradient(px, to_rgb(style.bg), to_rgb(style.bg_color2), (0, 0), (0, SIZE))
        else:
            layer = Image.new("RGBA", (px, px), (*to_rgb(style.bg), 255))
        img.paste(layer, (0, 0), _shape_mask(style.shape, px))
    return img


def _blur_alpha(mask: Image.Image, sigma: float, gain: float) -> Image.Image:
    m = mask.filter(ImageFilter.GaussianBlur(max(0.1, sigma)))
    return m.point(lambda v, g=gain: min(255, int(v * g)))


def _solid(px: int, rgb, alpha_mask: Image.Image) -> Image.Image:
    layer = Image.new("RGBA", (px, px), (*rgb, 255))
    layer.putalpha(alpha_mask)
    return layer


def _transform(layer: Image.Image, pose: Pose, k: float) -> Image.Image:
    """Масштаб, поворот и сдвиг всего слоя текста вокруг центра холста."""
    if pose.scale == 1.0 and pose.rot == 0.0 and pose.dx == 0.0 and pose.dy == 0.0:
        return layer
    px = layer.width
    c = px / 2
    th = math.radians(pose.rot)
    cs, sn = math.cos(th), math.sin(th)
    s = pose.scale
    dx, dy = pose.dx * k, pose.dy * k
    # PIL ждёт обратное преобразование: из выходной точки во входную
    a, b = cs / s, sn / s
    d, e = -sn / s, cs / s
    cx = c - a * (c + dx) - b * (c + dy)
    cy = c - d * (c + dx) - e * (c + dy)
    return layer.transform(layer.size, Image.AFFINE, (a, b, cx, d, e, cy), resample=Image.Resampling.BICUBIC)


class Scene:
    """Подготовленная сцена: фон и раскладка считаются один раз, кадры собираются быстро."""

    def __init__(self, style: Style, px: int):
        self.style = style
        self.px = px
        self.k = px / SIZE
        self.lay = layout_text(style.text, style.font, style.shape, shrink_factor(style))
        self.glyphs = glyph_list(self.lay, style.font)
        self.font = _pil_font(style.font, self.lay.size_px * self.k)
        self.bg = render_bg(style, px)
        self.per_glyph = style.anim in PER_GLYPH and style.fmt != "static"
        self._cache = None
        self.grad = None
        if style.grad:
            x0, y0, x1, y1 = self.lay.box
            self.grad = _gradient(px, to_rgb(style.fg), to_rgb(style.grad_color), (x0, y0), (x1, y1))

    def _masks(self, poses):
        st, k, px = self.style, self.k, self.px
        ow = round(OUTLINE_W.get(st.outline, 0) * k)
        gl = GLOW_LEVELS.get(st.glow)
        spread = round(gl["spread"] * k) if gl else 0
        fill = Image.new("L", (px, px), 0)
        df = ImageDraw.Draw(fill)
        out = Image.new("L", (px, px), 0) if ow else None
        do = ImageDraw.Draw(out) if out is not None else None
        gbase = Image.new("L", (px, px), 0) if gl else None
        dg = ImageDraw.Draw(gbase) if gbase is not None else None
        for (ch, x, base), (dy, a) in zip(self.glyphs, poses):
            if a <= 0:
                continue
            v = int(255 * a)
            xy = (x * k, (base + dy) * k)
            df.text(xy, ch, font=self.font, fill=v, anchor="ls")
            if do:
                do.text(xy, ch, font=self.font, fill=v, stroke_width=ow, stroke_fill=v, anchor="ls")
            if dg:
                dg.text(xy, ch, font=self.font, fill=v, stroke_width=ow + spread, stroke_fill=v, anchor="ls")
        return fill, out, gbase

    def _static_masks(self):
        if self._cache is None:
            self._cache = self._masks([(0.0, 1.0)] * len(self.glyphs))
        return self._cache

    def frame(self, t: float = 0.0) -> Image.Image:
        st, k, px = self.style, self.k, self.px
        pose = pose_at(st, t) if animated(st) else Pose()
        if self.per_glyph:
            n = len(self.glyphs)
            fill, out, gbase = self._masks([glyph_pose(st, t, i, n) for i in range(n)])
        else:
            fill, out, gbase = self._static_masks()

        layer = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        base_mask = out if out is not None else fill

        if st.shadow:
            sx, sy = SHADOW_OFFSET
            sm = ImageChops.offset(base_mask, round(sx * k), round(sy * k))
            sm = sm.filter(ImageFilter.GaussianBlur(3 * k)).point(lambda v: int(v * 0.55))
            layer.alpha_composite(_solid(px, (0, 0, 0), sm))

        gl = GLOW_LEVELS.get(st.glow)
        if gl and gbase is not None:
            ga = _blur_alpha(gbase, gl["r"] * k * 0.5, gl["gain"] * pose.glow_k)
            layer.alpha_composite(_solid(px, glow_rgb(st, pose.hue), ga))

        if out is not None:
            layer.alpha_composite(_solid(px, to_rgb(auto_outline(st)), out))

        if self.grad is not None and pose.hue is None:
            fl = self.grad.copy()
            fl.putalpha(fill)
        else:
            fl = _solid(px, text_rgb(st, pose.hue), fill)
        layer.alpha_composite(fl)

        layer = _transform(layer, pose, k)
        if pose.alpha < 1.0:
            layer.putalpha(layer.getchannel("A").point(lambda v, g=pose.alpha: int(v * g)))

        img = self.bg.copy()
        img.alpha_composite(layer)
        return img


def render_frame(style: Style, t: float = 0.0, px: int = 400) -> Image.Image:
    return Scene(style, px).frame(t)


def render_image(style: Style, px: int = 1024) -> Image.Image:
    """Статичная картинка px x px (первый кадр)."""
    return Scene(style, px).frame(0.0)


def render_frames(style: Style, px: int = 300, count: int | None = None) -> list[Image.Image]:
    n = count or loop_frames(style)
    sc = Scene(style, px)
    return [sc.frame(i / n) for i in range(n)]


# ════════════════════════════════════════════════════════════════
#  ВЫХОДНЫЕ ФАЙЛЫ
# ════════════════════════════════════════════════════════════════

def render_webp(style: Style, dst: str) -> str:
    big = render_frame(style, 0.0, 800)
    big.resize((100, 100), Image.Resampling.LANCZOS).save(dst, "WEBP", lossless=True, quality=100)
    return dst


def build_video(style: Style, dst: str) -> str:
    import media
    frames = render_frames(style, px=300)
    arr = np.stack([np.asarray(f.resize((100, 100), Image.Resampling.LANCZOS)) for f in frames])
    if not media._frames_to_webm(arr, 30.0, dst):
        raise RuntimeError("webm не удалось уложить в лимит")
    return dst


def build(style: Style, dst_dir: str) -> tuple[str, str, list[str]]:
    """-> (путь к файлу, формат Telegram: static | animated | video, заметки об упрощении)."""
    os.makedirs(dst_dir, exist_ok=True)
    notes: list[str] = []
    if style.fmt == "vector":
        import textlottie
        path = os.path.join(dst_dir, "text.tgs")
        _, notes = textlottie.build_tgs(style, path)
        return path, "animated", notes
    if style.fmt == "video":
        path = os.path.join(dst_dir, "text.webm")
        build_video(style, path)
        return path, "video", notes
    path = os.path.join(dst_dir, "text.webp")
    render_webp(style, path)
    return path, "static", notes


def clean_text(raw: str) -> str:
    t = " ".join((raw or "").replace("\n", " ").split())
    return t[:MAX_CHARS]


def missing_chars(text: str, font_key: str) -> str:
    """Символы, которых нет в выбранном шрифте (для понятного предупреждения)."""
    fm = FontMetrics(font_key)
    return "".join(sorted({c for c in text if c != " " and not fm.has(c)}))


# ════════════════════════════════════════════════════════════════
#  ПРЕВЬЮ ДЛЯ ЧАТА
# ════════════════════════════════════════════════════════════════

def _backdrop(size: int) -> Image.Image:
    """Тёмная подложка с лёгкой шахматкой, чтобы были видны прозрачный фон и светлый текст."""
    base = Image.new("RGBA", (size, size), (38, 40, 46, 255))
    d = ImageDraw.Draw(base)
    step = max(8, size // 20)
    for y in range(0, size, step):
        for x in range(0, size, step):
            if (x // step + y // step) % 2 == 0:
                d.rectangle((x, y, x + step - 1, y + step - 1), fill=(46, 48, 55, 255))
    return base


def render_preview_png(style: Style) -> io.BytesIO:
    img = render_frame(style, 0.0, 512)
    base = _backdrop(640)
    base.alpha_composite(img, (64, 64))
    buf = io.BytesIO()
    base.convert("RGB").save(buf, "PNG")
    buf.name = "preview.png"
    buf.seek(0)
    return buf


def _encode_mp4(raw: bytes, size: int, fps: float) -> io.BytesIO | None:
    import media
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "p.mp4")
        cmd = [media.ffmpeg_path, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{size}x{size}", "-r", f"{fps:g}", "-i", "-", "-an",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "26",
               "-movflags", "+faststart", out]
        res = subprocess.run(cmd, input=raw, capture_output=True)
        if res.returncode != 0 or not os.path.exists(out):
            return None
        with open(out, "rb") as f:
            buf = io.BytesIO(f.read())
    buf.name = "preview.mp4"
    buf.seek(0)
    return buf


def render_preview_anim(style: Style, tgs_path: str | None = None) -> io.BytesIO | None:
    """Зацикленное превью (mp4) для анимированных стилей. Для вектора кадры берутся из настоящего .tgs."""
    size, inner = 360, 288
    frames, fps = None, 30.0
    if style.fmt == "vector" and tgs_path:
        try:
            import vector
            arr, fps = vector.render_tgs(tgs_path, size=inner, max_frames=120, max_fps=30, supersample=2)
            frames = [Image.fromarray(a, "RGBA") for a in arr]
        except Exception:
            frames = None
    if frames is None:
        frames = render_frames(style, px=inner)
    raw = bytearray()
    off = (size - inner) // 2
    for f in frames:
        base = _backdrop(size)
        base.alpha_composite(f, (off, off))
        raw += base.convert("RGB").tobytes()
    return _encode_mp4(bytes(raw), size, fps)
