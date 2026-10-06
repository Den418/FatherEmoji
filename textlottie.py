"""
Векторные текстовые эмодзи (.tgs).

Буквы берутся из шрифта как контуры, поэтому остаются чёткими в любом размере, а файл
весит несколько КБ. Эффекты собираются средствами Lottie, которые понимает Telegram:

  свечение  - стопка полупрозрачных обводок разной ширины под буквами (настоящего размытия
              в Telegram нет, но стопка из 4-6 слоёв выглядит как мягкий ореол)
  обводка   - одна сплошная обводка под заливкой
  тень      - копия контуров со сдвигом, чёрная, полупрозрачная
  градиент  - линейная градиентная заливка
  анимации  - ключевые кадры слоя (масштаб, поворот, сдвиг, прозрачность),
              отдельных букв (волна, печать) и цвета (радуга, пульс свечения)

Если файл не влезает в 64 КБ, лишнее отключается по очереди, начиная с тени.
"""
import textemoji as T
import vector as V

ONE = {"x": [1], "y": [1]}
ZERO = {"x": [0], "y": [0]}
ONE_M = {"x": 1, "y": 1}
ZERO_M = {"x": 0, "y": 0}


def _r(x, d: int = 1):
    x = round(float(x), d)
    return int(x) if x == int(x) else x


def _static(v):
    return {"a": 0, "k": v}


def _kf(pairs, multi: bool = False) -> dict:
    """Линейные ключевые кадры. pairs: [(кадр, значение или список)]. multi: у свойства несколько компонент."""
    keys = []
    last = len(pairs) - 1
    for j, (f, v) in enumerate(pairs):
        vals = [_r(x, 3) for x in (v if isinstance(v, (list, tuple)) else [v])]
        d = {"t": _r(f, 2), "s": vals}
        if j < last:
            d["i"] = dict(ONE_M if multi else ONE)
            d["o"] = dict(ZERO_M if multi else ZERO)
        keys.append(d)
    return {"a": 1, "k": keys}


def _frames(total: int, step: int) -> list[int]:
    fr = list(range(0, total, step))
    if fr[-1] != total:
        fr.append(total)
    return fr


def _col(rgb) -> list:
    return [_r(rgb[0] / 255, 3), _r(rgb[1] / 255, 3), _r(rgb[2] / 255, 3), 1]


def _tr(p=None, o=None) -> dict:
    return {"ty": "tr", "p": p or _static([0, 0]), "a": _static([0, 0]), "s": _static([100, 100]),
            "r": _static(0), "o": o or _static(100), "sk": _static(0), "sa": _static(0)}


def _shapes(contours) -> list:
    return [{"ty": "sh", "ks": {"a": 0, "k": V._bezier(c)}} for c in contours]


def _glyph_contours(fm: T.FontMetrics, ch: str, size: float, x: float, base: float) -> list:
    pen = T._Collector(fm.gs)
    fm.gs[fm.glyph(ch)].draw(pen)
    k = size / fm.upm
    return [{
        # в шрифте ось Y смотрит вверх, на холсте вниз
        "v": [[x + px * k, base - py * k] for px, py in c["v"]],
        "i": [[a * k, -b * k] for a, b in c["i"]],
        "o": [[a * k, -b * k] for a, b in c["o"]],
    } for c in pen.contours]


# ════════════════════════════════════════════════════════════════
#  ЦВЕТ И ЗАЛИВКИ
# ════════════════════════════════════════════════════════════════

def _rainbow_keys(style: T.Style, total: int, n: int = 16) -> list:
    return [(round(total * k / n, 2), _col(T.hue_color(style.fg, k / n))) for k in range(n + 1)]


def _text_color_prop(style: T.Style, total: int) -> dict:
    if style.anim == "rainbow":
        return _kf(_rainbow_keys(style, total), multi=True)
    return _static(_col(T.to_rgb(style.fg)))


def _fill_item(style: T.Style, lay: T.Layout, total: int) -> dict:
    if style.anim != "rainbow" and style.grad:
        x0, y0, x1, y1 = lay.box
        c1, c2 = _col(T.to_rgb(style.fg)), _col(T.to_rgb(style.grad_color))
        return {"ty": "gf", "o": _static(100), "r": 1,
                "g": {"p": 2, "k": {"a": 0, "k": [0, *c1[:3], 1, *c2[:3]]}},
                "s": _static([_r(x0), _r(y0)]), "e": _static([_r(x1), _r(y1)]), "t": 1}
    return {"ty": "fl", "c": _text_color_prop(style, total), "o": _static(100), "r": 1}


def _glow_items(style: T.Style, total: int, step: int, n_force: int | None) -> list:
    gl = T.GLOW_LEVELS.get(style.glow)
    if not gl:
        return []
    n = n_force or gl["n"]
    ow = T.OUTLINE_W.get(style.outline, 0)
    if style.glow_color:
        color = _static(_col(T.to_rgb(style.glow_color)))
    else:
        color = _text_color_prop(style, total)
    frames = _frames(total, step) if style.anim == "glow" else None
    items = []
    for i in range(1, n + 1):
        width = 2 * (ow + gl["r"] * i / n)
        op = gl["op"] * 100 * (4 / n if n_force else 1)
        if frames:
            o = _kf([(f, min(100, op * T.pose_at(style, f / total).glow_k)) for f in frames])
        else:
            o = _static(_r(min(100, op)))
        items.append({"ty": "st", "c": color, "o": o, "w": _static(_r(width)), "lc": 2, "lj": 2, "ml": 4})
    return items


def _outline_item(style: T.Style) -> dict | None:
    if not style.outline:
        return None
    w = 2 * T.OUTLINE_W[style.outline]
    return {"ty": "st", "c": _static(_col(T.to_rgb(T.auto_outline(style)))), "o": _static(100),
            "w": _static(w), "lc": 2, "lj": 2, "ml": 4}


# ════════════════════════════════════════════════════════════════
#  ЕДИНИЦЫ АНИМАЦИИ И СЛОИ
# ════════════════════════════════════════════════════════════════

def _units(style: T.Style, lay: T.Layout, glyphs: list) -> list:
    """Один блок на весь текст, либо по блоку на каждую букву (волна и печать)."""
    fm = T.FontMetrics(style.font)
    per = [_glyph_contours(fm, ch, lay.size_px, x, base) for ch, x, base in glyphs]
    if style.anim in T.PER_GLYPH:
        n = len(glyphs)
        return [{"c": cs, "i": i, "n": n} for i, cs in enumerate(per) if cs]
    return [{"c": [c for cs in per for c in cs], "i": None, "n": len(glyphs)}]


def _unit_props(style: T.Style, unit: dict, total: int, step: int, offset=(0.0, 0.0)):
    ox, oy = offset
    p, o = _static([_r(ox), _r(oy)]), _static(100)
    i, n = unit["i"], unit["n"]
    if i is None:
        return p, o
    if style.anim == "wave":
        p = _kf([(f, [ox, oy + T.glyph_pose(style, f / total, i, n)[0]]) for f in _frames(total, step)], multi=True)
    elif style.anim == "typing":
        reset = T.TYPING_RESET * total
        ti = T.typing_start(i, n)
        f0, f1 = ti * total, (ti + 0.04) * total
        o = _kf([(0, 100), (reset, 100), (reset + 0.5, 0), (f0, 0), (f1, 100), (total, 100)])
    return p, o


def _layer_ks(style: T.Style, total: int, step: int) -> dict:
    ks = {"o": _static(100), "r": _static(0), "p": _static([256, 256, 0]),
          "a": _static([256, 256, 0]), "s": _static([100, 100, 100])}
    if style.anim not in ("pulse", "wobble", "bounce", "shake", "blink"):
        return ks
    frames = _frames(total, step)
    poses = [T.pose_at(style, f / total) for f in frames]

    def varies(vals):
        return max(vals) - min(vals) > 1e-6

    if varies([q.scale for q in poses]):
        ks["s"] = _kf([(f, [q.scale * 100, q.scale * 100, 100]) for f, q in zip(frames, poses)], multi=True)
    if varies([q.rot for q in poses]):
        ks["r"] = _kf([(f, q.rot) for f, q in zip(frames, poses)])
    if varies([q.dx for q in poses]) or varies([q.dy for q in poses]):
        ks["p"] = _kf([(f, [256 + q.dx, 256 + q.dy, 0]) for f, q in zip(frames, poses)], multi=True)
    if varies([q.alpha for q in poses]):
        ks["o"] = _kf([(f, q.alpha * 100) for f, q in zip(frames, poses)])
    return ks


def _bg_layer(style: T.Style, total: int, ind: int) -> dict | None:
    if not style.bg or style.shape == "none":
        return None
    S = T.SIZE
    if style.shape == "circle":
        geom = {"ty": "el", "d": 1, "s": _static([S, S]), "p": _static([S // 2, S // 2])}
    else:
        radius = 0 if style.shape == "square" else S * 0.24
        geom = {"ty": "rc", "d": 1, "s": _static([S, S]), "p": _static([S // 2, S // 2]), "r": _static(_r(radius))}
    if style.bg_grad:
        c1, c2 = _col(T.to_rgb(style.bg)), _col(T.to_rgb(style.bg_color2))
        fill = {"ty": "gf", "o": _static(100), "r": 1,
                "g": {"p": 2, "k": {"a": 0, "k": [0, *c1[:3], 1, *c2[:3]]}},
                "s": _static([S // 2, 0]), "e": _static([S // 2, S]), "t": 1}
    else:
        fill = {"ty": "fl", "c": _static(_col(T.to_rgb(style.bg))), "o": _static(100), "r": 1}
    ks = _layer_ks(T.Style(anim="none"), total, 2)
    return _layer(ind, [{"ty": "gr", "it": [geom, fill, _tr()]}], ks, total)


def _layer(ind: int, shapes: list, ks: dict, total: int) -> dict:
    return {"ddd": 0, "ind": ind, "ty": 4, "nm": f"l{ind}", "sr": 1, "ks": ks, "ao": 0,
            "shapes": shapes, "ip": 0, "op": total, "st": 0, "bm": 0}


def _document(style: T.Style, lay: T.Layout, glyphs: list, total: int, step: int,
              shadow: bool, glow_n: int | None) -> dict:
    units = _units(style, lay, glyphs)
    fill = _fill_item(style, lay, total)
    outline = _outline_item(style)
    glow = _glow_items(style, total, step, glow_n)

    main, shades = [], []
    for u in units:
        p, o = _unit_props(style, u, total, step)
        items = _shapes(u["c"]) + [fill]
        if outline:
            items.append(outline)
        items += glow
        items.append(_tr(p, o))
        main.append({"ty": "gr", "it": items})

        if shadow:
            sp, so = _unit_props(style, u, total, step, offset=T.SHADOW_OFFSET)
            ow = T.OUTLINE_W.get(style.outline, 0)
            sitems = _shapes(u["c"]) + [
                {"ty": "fl", "c": _static([0, 0, 0, 1]), "o": _static(42), "r": 1},
                {"ty": "st", "c": _static([0, 0, 0, 1]), "o": _static(14), "w": _static(2 * ow + 9),
                 "lc": 2, "lj": 2, "ml": 4},
                _tr(sp, so),
            ]
            shades.append({"ty": "gr", "it": sitems})

    layers = [_layer(1, main + shades, _layer_ks(style, total, step), total)]
    bg = _bg_layer(style, total, 2)
    if bg:
        layers.append(bg)
    return {"v": "5.5.2", "fr": 30, "ip": 0, "op": total, "w": T.SIZE, "h": T.SIZE, "nm": "t",
            "ddd": 0, "assets": [], "layers": layers, "markers": []}


# ════════════════════════════════════════════════════════════════
#  ПУБЛИЧНЫЙ API
# ════════════════════════════════════════════════════════════════

def build_tgs(style: T.Style, dst: str) -> tuple[int, list[str]]:
    """
    Собирает .tgs. Если не влезает в лимит, по очереди упрощает: реже ключевые кадры,
    без тени, мягче свечение, без свечения, без обводки.
    -> (размер, что пришлось отключить). Бросает vector.ImageTooComplex, если не помогло.
    """
    lay = T.layout_text(style.text, style.font, style.shape, T.shrink_factor(style))
    glyphs = T.glyph_list(lay, style.font)
    total = T.loop_frames(style) if T.animated(style) else 30
    s0 = 1 if style.anim == "shake" else 2

    plans = [
        (dict(step=s0), None),
        (dict(step=s0 + 1), None),
        (dict(step=s0 + 2, shadow=False), "тень"),
        (dict(step=s0 + 2, shadow=False, glow_n=3), "часть слоёв свечения"),
        (dict(step=s0 + 3, shadow=False, glow=0), "свечение"),
        (dict(step=s0 + 3, shadow=False, glow=0, outline=0), "обводка"),
    ]
    notes: list[str] = []
    for opts, label in plans:
        if label and label not in notes:
            notes.append(label)
        st = style
        if "glow" in opts or "outline" in opts:
            st = style.copy()
            st.glow = opts.get("glow", st.glow)
            st.outline = opts.get("outline", st.outline)
        doc = _document(st, lay, glyphs, total, opts["step"],
                        shadow=style.shadow and opts.get("shadow", True),
                        glow_n=opts.get("glow_n"))
        size = V.write_tgs(doc, dst)
        if size <= V.TGS_TARGET:
            # заметки показываем только о том, что реально отключено и было включено
            real = [n for n in notes if
                    (n == "тень" and style.shadow) or (n == "часть слоёв свечения" and style.glow) or
                    (n == "свечение" and style.glow) or (n == "обводка" and style.outline)]
            if "свечение" in real and "часть слоёв свечения" in real:
                real.remove("часть слоёв свечения")
            return size, real
    raise V.ImageTooComplex()
