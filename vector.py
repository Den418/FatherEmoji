"""
Векторные эмодзи: растр -> .tgs, сжатие тяжёлых .tgs, рендер .tgs в кадры.

Что здесь исправлено по сравнению с первой версией:
  * vtracer учитывает только альфу ровно 0, а полупрозрачные пиксели считает
    непрозрачными вместе с их "мусорным" RGB. Отсюда белые пятна и чёрные
    обводки вокруг теней. Теперь альфа бинаризуется заранее, а цвета под
    краями и прозрачностью подменяются цветом ближайшей сплошной области.
  * Палитра строится самим ботом (а не битовой маской vtracer), поэтому
    светлые цвета больше не "схлопываются" в белый.
  * Lottie собирается напрямую, без пакета lottie: координаты округляются,
    лишние поля не пишутся, файл получается в 2-4 раза легче и влезает
    в лимит 64 КБ с гораздо большей детализацией.
  * Маленькие картинки теперь увеличиваются до холста 512, а не лежат
    крошечным пятном посередине.
"""
import gzip
import json
import logging
import os
import re
import tempfile

import numpy as np
from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger("fatheremoji")

TGS_LIMIT = 64 * 1024      # жёсткий лимит .tgs у Telegram
TGS_TARGET = 61 * 1024     # целимся чуть ниже, чтобы был запас
CANVAS = 512
SS = 2                     # рабочий холст в SS раз больше: контуры получаются плавнее
WORK = CANVAS * SS
EDGE = 3 * SS              # прозрачная рамка, чтобы vtracer включил режим прозрачности


class VectorizeError(Exception):
    pass


class VectorizerNotInstalled(VectorizeError):
    pass


class ImageTooComplex(VectorizeError):
    pass


# ════════════════════════════════════════════════════════════════
#  ПОДГОТОВКА КАРТИНКИ
# ════════════════════════════════════════════════════════════════

def _load_rgba(src) -> Image.Image:
    img = src if isinstance(src, Image.Image) else Image.open(src)
    try:
        img.seek(0)
    except Exception:
        pass
    img = ImageOps.exif_transpose(img)
    return img.convert("RGBA")


def _fit_canvas(img: Image.Image, crop: bool = True) -> Image.Image:
    """Вписывает картинку в 512x512 (с увеличением маленьких). Прозрачные поля обрезаются."""
    a = np.asarray(img.getchannel("A"))
    has_alpha = bool((a < 128).any())

    if crop and has_alpha:
        ys, xs = np.where(a > 16)
        if len(xs) == 0:
            raise VectorizeError("картинка полностью прозрачная")
        img = img.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))

    w, h = img.size
    square = abs(w - h) <= max(2, 0.01 * max(w, h))
    border = EDGE if (has_alpha or not square) else 0
    inner = WORK - 2 * border
    scale = min(inner / w, inner / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (WORK, WORK), (0, 0, 0, 0))
    canvas.paste(img, ((WORK - nw) // 2, (WORK - nh) // 2))
    return canvas


def _bleed(rgb: np.ndarray, known: np.ndarray, iters: int = 14) -> np.ndarray:
    """Растягивает цвета известных пикселей на соседние неизвестные (убирает каймы)."""
    rgb = rgb.astype(np.float32).copy()
    known = known.copy()
    offsets = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]
    for _ in range(iters):
        if known.all():
            break
        acc = np.zeros_like(rgb)
        cnt = np.zeros(known.shape, np.float32)
        for dy, dx in offsets:
            sk = np.roll(known, (dy, dx), (0, 1))
            sr = np.roll(rgb, (dy, dx), (0, 1))
            acc += sr * sk[..., None]
            cnt += sk
        grow = (~known) & (cnt > 0)
        rgb[grow] = acc[grow] / cnt[grow][..., None]
        known |= grow
    if not known.all() and known.any():
        rgb[~known] = rgb[known].mean(axis=0)
    return rgb


def _prepare(src, crop: bool = True):
    """-> (rgb, mask, alpha, raw). mask: где картинка достаточно непрозрачна; raw: исходные цвета без подмены."""
    canvas = _fit_canvas(_load_rgba(src), crop=crop)
    arr = np.asarray(canvas)
    alpha = arr[..., 3]
    mask = alpha >= 128
    if not mask.any():
        raise VectorizeError("картинка полностью прозрачная")
    # Краевая полоса: пиксели рядом с прозрачностью. Их цвет ненадёжен (смесь с фоном),
    # поэтому берём цвет ближайшей сплошной области. А вот полупрозрачная середина
    # картинки (блики, стекло) сохраняет свои собственные цвета.
    inner = np.asarray(Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(4 * SS + 1))) > 0
    edge = mask & ~inner
    solid = alpha >= 250
    known = (solid & ~edge) | (mask & ~edge)
    if known.sum() < 0.2 * mask.sum():      # крошечная фигурка: края и есть вся картинка
        known = mask
    rgb = _bleed(arr[..., :3], known, iters=SS * 8)
    return rgb, mask, alpha, arr[..., :3].astype(np.float32)


def _nearest(rgb: np.ndarray, palette: np.ndarray) -> np.ndarray:
    flat = rgb.reshape(-1, 3)
    best = np.full(flat.shape[0], np.inf, np.float32)
    idx = np.zeros(flat.shape[0], np.int32)
    for i, c in enumerate(palette.astype(np.float32)):
        d = ((flat - c) ** 2).sum(axis=1)
        better = d < best
        best[better] = d[better]
        idx[better] = i
    return idx.reshape(rgb.shape[:2])


def _palette_error(px: np.ndarray, pal: np.ndarray) -> float:
    best = np.full(len(px), np.inf, np.float32)
    for c in pal.astype(np.float32):
        best = np.minimum(best, np.abs(px.astype(np.float32) - c).sum(axis=1))
    return float(np.percentile(best, 92))          # игнорируем 8% самых "спорных" пикселей (края)


def auto_colors(rgb: np.ndarray, mask: np.ndarray, cap: int) -> int:
    """Минимум цветов, при котором картинка уже не страдает: у плоских эмодзи это 6-12, у фото упрёмся в cap."""
    px = np.clip(rgb[mask], 0, 255).astype(np.uint8)
    if len(px) > 20000:
        px = px[np.random.RandomState(3).choice(len(px), 20000, replace=False)]
    for n in (3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48):
        if n > cap:
            break
        pal = _build_palette(rgb, mask, n)
        if _palette_error(px, pal) <= 14:
            return n
    return cap


def _smooth_labels(idx: np.ndarray, n: int, sigma: float) -> np.ndarray:
    """Мажоритарное сглаживание карты цветов: убирает прослойки толщиной в 1-2 px от сглаживания краёв."""
    if n < 2 or n > 40:
        return idx
    best = None
    out = np.zeros(idx.shape, np.int32)
    for k in range(n):
        m = Image.fromarray(((idx == k) * 255).astype(np.uint8))
        w = np.asarray(m.filter(ImageFilter.GaussianBlur(sigma)), np.float32)
        if best is None:
            best = w
        else:
            better = w > best
            best = np.where(better, w, best)
            out[better] = k
    return out


def _to_lab(px: np.ndarray) -> np.ndarray:
    f = px.astype(np.float32) / 255
    lin = np.where(f <= 0.04045, f / 12.92, ((f + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]], np.float32)
    xyz = (lin @ m.T) / np.array([0.9505, 1.0, 1.089], np.float32)
    g = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * g[:, 1] - 16, 500 * (g[:, 0] - g[:, 1]), 200 * (g[:, 1] - g[:, 2])], 1)


def _refine(px: np.ndarray, init: np.ndarray, iters: int = 6) -> np.ndarray:
    """k-means в перцептивном пространстве. Насыщенные пиксели весят больше, чтобы
    глаза, губы и цветы не терялись под большими однотонными областями."""
    lab = _to_lab(px)
    w = 1.0 + np.hypot(lab[:, 1], lab[:, 2]) / 20.0
    cent = np.array([lab[((px.astype(np.float32) - c) ** 2).sum(1).argmin()] for c in init], np.float32)
    for _ in range(iters):
        a = ((lab[:, None, :] - cent[None, :, :]) ** 2).sum(2).argmin(1)
        for k in range(len(cent)):
            m = a == k
            if m.any():
                cent[k] = (lab[m] * w[m, None]).sum(0) / w[m].sum()
    a = ((lab[:, None, :] - cent[None, :, :]) ** 2).sum(2).argmin(1)
    out = init.astype(np.float32).copy()
    for k in range(len(cent)):
        m = a == k
        if m.any():
            out[k] = (px[m].astype(np.float32) * w[m, None]).sum(0) / w[m].sum()
    return out.round().clip(0, 255).astype(np.uint8)


def _build_palette(rgb: np.ndarray, mask: np.ndarray, n: int) -> np.ndarray:
    px = np.clip(rgb[mask], 0, 255).astype(np.uint8)
    # Подвыборка: для медианного среза хватает нескольких десятков тысяч точек.
    if len(px) > 24000:
        px = px[np.random.RandomState(7).choice(len(px), 24000, replace=False)]
    tmp = Image.fromarray(px.reshape(-1, 1, 3), "RGB")
    q = tmp.quantize(colors=n, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = np.array(q.getpalette()[: 3 * n], np.uint8).reshape(-1, 3)
    used = sorted({c for _, c in (q.getcolors() or [])})
    pal = pal[used] if used else pal[:1]
    return _refine(px, pal) if len(pal) > 2 else pal


# ════════════════════════════════════════════════════════════════
#  SVG -> LOTTIE
# ════════════════════════════════════════════════════════════════

_PATH_RE = re.compile(r"<path\b([^>]*)/?>", re.S)
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')
_TOK_RE = re.compile(r"[MCLZmclz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _n(x: float):
    x = round(float(x), 1)
    return int(x) if x == int(x) else x


def _parse_d(d: str, ox: float, oy: float, k: float = 1.0):
    """Разбирает path vtracer (M C L Z, абсолютные) в список контуров Lottie. k: масштаб координат."""
    toks = _TOK_RE.findall(d)
    contours = []
    cur = None
    i = 0
    cmd = None

    def num(k):
        return [float(t) for t in toks[i:i + k]]

    while i < len(toks):
        t = toks[i]
        if t in "MCLZmclz":
            cmd = t.upper()
            i += 1
            if cmd == "Z":
                if cur and len(cur["v"]) > 1:
                    v, ii, oo = cur["v"], cur["i"], cur["o"]
                    if abs(v[-1][0] - v[0][0]) < 0.05 and abs(v[-1][1] - v[0][1]) < 0.05:
                        ii[0] = ii[-1]
                        v.pop(); ii.pop(); oo.pop()
                    if len(v) > 1:
                        contours.append(cur)
                cur = None
            continue
        if cmd == "M":
            x, y = num(2); i += 2
            cur = {"v": [[x + ox, y + oy]], "i": [[0, 0]], "o": [[0, 0]]}
            cmd = "L"          # следующие пары после M считаются линиями
        elif cmd == "L":
            x, y = num(2); i += 2
            if cur is None:
                continue
            cur["v"].append([x + ox, y + oy]); cur["i"].append([0, 0]); cur["o"].append([0, 0])
        elif cmd == "C":
            x1, y1, x2, y2, x, y = num(6); i += 6
            if cur is None:
                continue
            px, py = cur["v"][-1]
            cur["o"][-1] = [x1 + ox - px, y1 + oy - py]
            cur["v"].append([x + ox, y + oy])
            cur["i"].append([x2 + ox - (x + ox), y2 + oy - (y + oy)])
            cur["o"].append([0, 0])
        else:
            i += 1
    if k != 1.0:
        for c in contours:
            for key in ("v", "i", "o"):
                c[key] = [[a * k, b * k] for a, b in c[key]]
    return contours


def _bezier(c) -> dict:
    return {
        "c": True,
        "i": [[_n(a), _n(b)] for a, b in c["i"]],
        "o": [[_n(a), _n(b)] for a, b in c["o"]],
        "v": [[_n(a), _n(b)] for a, b in c["v"]],
    }


_TR = {
    "ty": "tr",
    "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]},
    "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0},
    "o": {"a": 0, "k": 100}, "sk": {"a": 0, "k": 0}, "sa": {"a": 0, "k": 0},
}


def _color(rgb) -> list:
    return [round(rgb[0] / 255, 3), round(rgb[1] / 255, 3), round(rgb[2] / 255, 3), 1]


def _group_paths(contours, rgb, rule: int = 2, opacity: int = 100) -> dict:
    """rule: 2 = even-odd (контуры vtracer), 1 = non-zero (буквы шрифта с перекрытиями)."""
    items = [{"ty": "sh", "ks": {"a": 0, "k": _bezier(c)}} for c in contours]
    items.append({"ty": "fl", "c": {"a": 0, "k": _color(rgb)}, "o": {"a": 0, "k": opacity}, "r": rule})
    items.append(dict(_TR))
    return {"ty": "gr", "it": items}


def group_rect(cx, cy, w, h, radius, rgb) -> dict:
    """Скруглённый прямоугольник (фон для текстовых эмодзи)."""
    return {"ty": "gr", "it": [
        {"ty": "rc", "d": 1, "s": {"a": 0, "k": [_n(w), _n(h)]},
         "p": {"a": 0, "k": [_n(cx), _n(cy)]}, "r": {"a": 0, "k": _n(radius)}},
        {"ty": "fl", "c": {"a": 0, "k": _color(rgb)}, "o": {"a": 0, "k": 100}, "r": 1},
        dict(_TR),
    ]}


def lottie_doc(groups_top_first: list, frames: int = 30, fr: int = 30) -> dict:
    return {
        "v": "5.5.2", "fr": fr, "ip": 0, "op": frames, "w": CANVAS, "h": CANVAS,
        "nm": "e", "ddd": 0, "assets": [],
        "layers": [{
            "ddd": 0, "ind": 1, "ty": 4, "nm": "e", "sr": 1,
            "ks": {"o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                   "p": {"a": 0, "k": [256, 256, 0]}, "a": {"a": 0, "k": [256, 256, 0]},
                   "s": {"a": 0, "k": [100, 100, 100]}},
            "ao": 0, "shapes": groups_top_first, "ip": 0, "op": frames, "st": 0, "bm": 0,
        }],
        "markers": [],
    }


def write_tgs(doc: dict, dst: str) -> int:
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
    with gzip.GzipFile(dst, "wb", compresslevel=9, mtime=0) as f:
        f.write(raw)
    return os.path.getsize(dst)


# Профили: от максимальной детализации к самому лёгкому.
# (цветов в палитре, мин. размер пятна, порог длины сегмента, порог угла, splice)
LEVELS = [
    (48, 2, 3.5, 50, 40),
    (36, 3, 4.0, 55, 45),
    (28, 4, 4.0, 60, 45),
    (20, 6, 4.5, 60, 50),
    (14, 8, 5.0, 65, 55),
    (10, 12, 6.0, 70, 60),
    (7, 18, 7.0, 75, 60),
    (5, 26, 8.0, 80, 65),
    (4, 40, 9.0, 85, 70),
]


def _trace(png_path: str, svg_path: str, lvl, binary: bool = False):
    import vtracer  # ленивый импорт: без vtracer бот живёт, векторный режим сообщит об этом
    _, speckle, length, corner, splice = lvl
    vtracer.convert_image_to_svg_py(
        png_path, svg_path,
        colormode="binary" if binary else "color",
        hierarchical="stacked", mode="spline",
        filter_speckle=int(speckle * SS), color_precision=8, layer_difference=2,
        corner_threshold=corner, length_threshold=length * SS, max_iterations=10,
        splice_threshold=splice, path_precision=2,
    )


def _svg_to_groups(svg_path: str, forced_color=None, opacity_by_hex: dict | None = None) -> list:
    """SVG vtracer -> группы Lottie (верхний слой первым). opacity_by_hex: оставить только эти цвета, с их прозрачностью."""
    with open(svg_path, encoding="utf-8") as f:
        svg = f.read()
    groups = []
    for m in _PATH_RE.finditer(svg):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        d = attrs.get("d", "")
        fill = attrs.get("fill", "#000000").strip()
        if not d or not re.fullmatch(r"#[0-9A-Fa-f]{6}", fill):
            continue
        ox = oy = 0.0
        tm = re.search(r"translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)", attrs.get("transform", ""))
        if tm:
            ox, oy = float(tm.group(1)), float(tm.group(2))
        contours = _parse_d(d, ox, oy, 1.0 / SS)
        if not contours:
            continue
        rgb = forced_color or tuple(int(fill[k:k + 2], 16) for k in (1, 3, 5))
        if opacity_by_hex is not None:
            op = opacity_by_hex.get(fill.upper())
            if op is None:
                continue
            groups.append(_group_paths(contours, rgb, opacity=op))
        else:
            groups.append(_group_paths(contours, rgb))
    groups.reverse()       # в SVG первым рисуется нижний слой, в Lottie первым идёт верхний
    return groups


# ════════════════════════════════════════════════════════════════
#  ПОЛУПРОЗРАЧНЫЕ ОБЛАСТИ (стекло, свечение)
# ════════════════════════════════════════════════════════════════

TR_LO, TR_MIN_AREA = 40, 0.012      # альфа ниже 40 считаем шумом; слой нужен, если занимает >1.2% холста


def _translucent_groups(rgb: np.ndarray, alpha: np.ndarray, tmp: str) -> list:
    """
    Толстые области с альфой 40..127 рисуются отдельным слоем с настоящей прозрачностью.
    Тонкие каймы сглаживания и мягкие тёмные тени отбрасываются: плоская полупрозрачная
    клякса с жёстким краем вместо мягкой тени выглядит хуже, чем её отсутствие.
    """
    band = (alpha >= TR_LO) & (alpha < 128)
    if band.mean() < TR_MIN_AREA:
        return []
    k = 4 * SS + 1
    m = Image.fromarray((band * 255).astype(np.uint8))
    core = np.asarray(m.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k))) > 0
    core &= band
    if core.mean() < TR_MIN_AREA:
        return []

    pal = _build_palette(rgb, core, 3)
    idx = _smooth_labels(_nearest(rgb, pal), len(pal), 0.9 * SS)
    keep_hex, opacity = {}, {}
    for i, c in enumerate(pal):
        cls = core & (idx == i)
        if cls.sum() < 0.004 * core.size:
            continue
        lum = float(0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255
        chroma = (int(c.max()) - int(c.min())) / 255
        if lum < 0.28 and chroma < 0.18:          # серая или чёрная тень
            continue
        h = "#{:02X}{:02X}{:02X}".format(*(int(x) for x in c))
        keep_hex[h] = i
        opacity[h] = int(round(float(alpha[cls].mean()) / 255 * 100))
    if not keep_hex:
        return []

    out = np.where(core[..., None], pal[idx], 0).astype(np.uint8)
    rgba = np.dstack([out, np.where(core, 255, 0).astype(np.uint8)])
    png, svg = os.path.join(tmp, "t.png"), os.path.join(tmp, "t.svg")
    Image.fromarray(rgba, "RGBA").save(png)
    try:
        _trace(png, svg, LEVELS[4])
        groups = _svg_to_groups(svg, opacity_by_hex=opacity)
    except Exception as e:
        log.warning(f"Слой полупрозрачности не построился: {e}")
        return []
    return groups


# ════════════════════════════════════════════════════════════════
#  ПУБЛИЧНЫЙ API: РАСТР -> TGS
# ════════════════════════════════════════════════════════════════

def vectorize_sync(src, dst: str, fixed_palette=None, crop: bool = True,
                   extra_groups_bottom=None) -> str:
    """
    src: путь или PIL.Image. fixed_palette: список RGB, если цвета известны заранее
    (текстовые эмодзи). extra_groups_bottom: готовые группы Lottie, которые кладутся
    под трассированные контуры (например, фон-прямоугольник).

    Уровень качества ищется двоичным поиском: размер файла монотонно падает с уровнем,
    поэтому хватает 3-4 попыток вместо перебора всей лестницы.
    """
    try:
        import vtracer  # noqa: F401
    except ImportError as e:
        raise VectorizerNotInstalled(str(e))

    rgb, mask, alpha, raw = _prepare(src, crop=crop)
    auto = auto_colors(rgb, mask, 48) if fixed_palette is None else 0
    levels = LEVELS if fixed_palette is None else LEVELS[:1]

    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "q.png")
        svg = os.path.join(tmp, "q.svg")
        cache = {}
        glass = _translucent_groups(raw, alpha, tmp) if fixed_palette is None else []

        def attempt(i):
            """-> (size, doc) либо (None, None), если уровень не удался."""
            lvl = levels[i]
            n_colors = min(lvl[0], auto) if fixed_palette is None else 0
            key = (n_colors, lvl[1:])
            if key in cache:
                return cache[key]
            pal = (np.array(fixed_palette, np.uint8) if fixed_palette is not None
                   else _build_palette(rgb, mask, n_colors))
            idx = _smooth_labels(_nearest(rgb, pal), len(pal), 0.9 * SS)
            rgba = np.dstack([pal[idx], np.where(mask, 255, 0).astype(np.uint8)])
            Image.fromarray(rgba, "RGBA").save(png)
            res = (None, None)
            try:
                _trace(png, svg, lvl)
                groups = _svg_to_groups(svg)
                if groups:
                    doc = lottie_doc(list(groups) + list(glass) + list(extra_groups_bottom or []))
                    size = write_tgs(doc, os.path.join(tmp, "t.tgs"))
                    res = (size, doc)
            except ImportError as e:
                raise VectorizerNotInstalled(str(e))
            except Exception as e:
                log.warning(f"Профиль векторизации {lvl} не прошёл: {e}")
            cache[key] = res
            return res

        size, doc = attempt(0)
        best = doc if (size is not None and size <= TGS_TARGET) else None
        if best is None:
            lo, hi = 1, len(levels) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                size, doc = attempt(mid)
                if size is not None and size <= TGS_TARGET:
                    best, hi = doc, mid - 1
                else:
                    lo = mid + 1
        if best is None:      # запасной вариант: что-нибудь в пределах жёсткого лимита
            for i in range(len(levels) - 1, -1, -1):
                size, doc = attempt(i)
                if size is not None and size <= TGS_LIMIT:
                    best = doc
                    break
        if best is None:
            raise ImageTooComplex()
        write_tgs(best, dst)
        return dst


# ════════════════════════════════════════════════════════════════
#  СЖАТИЕ ГОТОВОГО .TGS
# ════════════════════════════════════════════════════════════════

_DROP_KEYS = {"nm", "mn", "ln", "cl", "tg", "hd"}


def _slim(node, dec: int):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _DROP_KEYS:
                if k == "hd" and v:
                    out[k] = v
                continue
            if k == "c" and isinstance(v, dict):
                out[k] = _slim(v, 3)               # цвета: тонкая точность
            elif k == "g" and isinstance(v, dict):
                out[k] = _slim(v, 3)               # градиенты
            elif k in ("i", "o") and isinstance(v, dict):
                out[k] = _slim(v, 3)               # кривые анимации (easing)
            elif k == "t":
                out[k] = _slim(v, 2)               # время ключевых кадров
            else:
                out[k] = _slim(v, dec)
        return out
    if isinstance(node, list):
        return [_slim(x, dec) for x in node]
    if isinstance(node, float):
        r = round(node, dec)
        return int(r) if r == int(r) else r
    return node


def load_tgs(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read()
    try:
        data = gzip.decompress(data)
    except OSError:
        pass
    return json.loads(data)


def optimize_tgs(src: str, dst: str, limit: int = TGS_LIMIT) -> int:
    """
    Ужимает .tgs до лимита без потери кадров: убирает служебные поля и снижает
    точность чисел ступенями. Возвращает итоговый размер или бросает ImageTooComplex.
    """
    doc = load_tgs(src)
    fr = float(doc.get("fr", 30))
    frames = float(doc.get("op", 0)) - float(doc.get("ip", 0))
    if frames / max(fr, 1) > 3.05:
        raise ImageTooComplex("анимация дольше 3 секунд")
    if int(doc.get("w", 512)) != 512 or int(doc.get("h", 512)) != 512:
        doc["w"] = doc["h"] = 512

    last = None
    for dec in (3, 2, 1):
        slim = _slim(doc, dec)
        size = write_tgs(slim, dst)
        last = size
        if size <= limit:
            return size
    if os.path.exists(dst):
        os.remove(dst)
    raise ImageTooComplex(f"{last} байт после сжатия")


# ════════════════════════════════════════════════════════════════
#  РЕНДЕР TGS В КАДРЫ (нужен rlottie-python; без него конвертации анимаций недоступны)
# ════════════════════════════════════════════════════════════════

class RenderUnavailable(Exception):
    pass


def _render_frame(anim, n: int, size: int, supersample: int = 2) -> Image.Image:
    """
    Кадр .tgs -> RGBA с обычной (не премультиплицированной) альфой.
    rlottie отдаёт премультиплицированные цвета, а Pillow читает их как обычные,
    из-за чего края и полупрозрачные места темнеют. Поэтому сжимаем кадр в режиме
    RGBa (премультиплицированный) и только потом переводим в RGBA.
    """
    big = size * supersample
    raw = anim.render_pillow_frame(frame_num=n, width=big, height=big).convert("RGBA")
    pm = Image.frombytes("RGBa", raw.size, raw.tobytes())
    if supersample > 1:
        pm = pm.resize((size, size), Image.Resampling.LANCZOS)
    return pm.convert("RGBA")


def render_tgs(path: str, size: int = 100, max_frames: int = 90, max_fps: int = 30,
               supersample: int = 2):
    """-> (frames: ndarray [N,size,size,4] uint8, fps)."""
    try:
        from rlottie_python import LottieAnimation
    except ImportError as e:
        raise RenderUnavailable(str(e))

    anim = LottieAnimation.from_tgs(path)
    total = anim.lottie_animation_get_totalframe()
    src_fps = float(anim.lottie_animation_get_framerate() or 30)
    fps = min(float(max_fps), src_fps)
    step = max(1.0, src_fps / fps)
    frames = []
    pos = 0.0
    while int(pos) < total and len(frames) < max_frames:
        frames.append(np.asarray(_render_frame(anim, int(pos), size, supersample)))
        pos += step
    if not frames:
        raise RenderUnavailable("пустая анимация")
    return np.stack(frames), fps


def tgs_poster(path: str, size: int = 100) -> Image.Image:
    """Один representative-кадр .tgs (для превращения анимации в статику)."""
    try:
        from rlottie_python import LottieAnimation
    except ImportError as e:
        raise RenderUnavailable(str(e))
    anim = LottieAnimation.from_tgs(path)
    total = max(1, anim.lottie_animation_get_totalframe())
    best, best_cover = None, -1
    # Берём кадр с наибольшим покрытием из нескольких: первый кадр часто пустой.
    for k in (0, total // 4, total // 2, (3 * total) // 4):
        im = _render_frame(anim, min(k, total - 1), size)
        cover = int((np.asarray(im.getchannel("A")) > 16).sum())
        if cover > best_cover:
            best, best_cover = im, cover
    return best
