"""
Перекраска эмодзи: статика (webp), анимации (.tgs), видео (webm).

Два режима:
  mono - всё становится оттенками одного цвета. Светлые места получают чистый цвет,
         тёмные темнеют, поэтому объём и контуры сохраняются.
  hue  - сдвиг оттенка. Разноцветный эмодзи остаётся разноцветным, но весь его набор
         цветов поворачивается так, чтобы основной цвет стал выбранным.
"""
import colorsys
import json
import os
import subprocess

import numpy as np
from PIL import Image

import media
import vector

FLOOR = 0.12          # самые тёмные места остаются чуть светлее чёрного
MIN_CHROMA = 0.15     # пиксели бледнее этого не участвуют в поиске основного цвета


def _hex_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _lum(rgb: np.ndarray) -> np.ndarray:
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


# ════════════════════════════════════════════════════════════════
#  ОБЩАЯ МАТЕМАТИКА НА МАССИВАХ RGB (0..1)
# ════════════════════════════════════════════════════════════════

def _rgb_to_hsv(rgb: np.ndarray):
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    d = mx - mn
    s = np.where(mx > 0, d / np.maximum(mx, 1e-9), 0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    dd = np.maximum(d, 1e-9)
    h = np.where(mx == r, ((g - b) / dd) % 6,
                 np.where(mx == g, (b - r) / dd + 2, (r - g) / dd + 4)) / 6.0
    h = np.where(d == 0, 0, h)
    return h % 1.0, s, mx


def _hsv_to_rgb(h, s, v):
    i = np.floor(h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def _dominant_hue(rgb: np.ndarray, weight: np.ndarray):
    """Основной оттенок как круговое среднее. None, если эмодзи по сути серый."""
    h, s, v = _rgb_to_hsv(rgb)
    w = weight * s * v * (s > MIN_CHROMA)
    if w.sum() < 1e-6:
        return None
    ang = h * 2 * np.pi
    x, y = (w * np.cos(ang)).sum(), (w * np.sin(ang)).sum()
    if abs(x) + abs(y) < 1e-9:
        return None
    return (np.arctan2(y, x) / (2 * np.pi)) % 1.0


def apply_mono(rgb: np.ndarray, weight: np.ndarray, target) -> np.ndarray:
    lum = _lum(rgb)
    vis = weight > 0.02
    if vis.any():
        lo, hi = lum[vis].min(), lum[vis].max()
    else:
        lo, hi = 0.0, 1.0
    norm = np.clip((lum - lo) / (hi - lo), 0, 1) if hi > lo else np.ones_like(lum)
    k = FLOOR + (1 - FLOOR) * norm
    return np.clip(k[..., None] * np.array(target, np.float32), 0, 1)


def apply_hue(rgb: np.ndarray, weight: np.ndarray, target):
    """-> новый rgb либо None, если режим неприменим (серый эмодзи или серая цель)."""
    th, ts, tv = colorsys.rgb_to_hsv(*target)
    if ts < 0.12:
        return None
    dom = _dominant_hue(rgb, weight)
    if dom is None:
        return None
    h, s, v = _rgb_to_hsv(rgb)
    return np.clip(_hsv_to_rgb((h + (th - dom)) % 1.0, s, v), 0, 1)


def recolor_array(rgb: np.ndarray, weight: np.ndarray, hex_color: str, mode: str) -> np.ndarray:
    target = _hex_rgb(hex_color)
    if mode == "hue":
        out = apply_hue(rgb, weight, target)
        if out is not None:
            return out
    return apply_mono(rgb, weight, target)


# ════════════════════════════════════════════════════════════════
#  СТАТИКА
# ════════════════════════════════════════════════════════════════

def recolor_image(src: str, dst: str, hex_color: str, mode: str) -> str:
    img = Image.open(src).convert("RGBA")
    img.thumbnail((100, 100), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    canvas.paste(img, ((100 - img.width) // 2, (100 - img.height) // 2))
    arr = np.asarray(canvas).astype(np.float32) / 255
    rgb, a = arr[..., :3], arr[..., 3]
    out = recolor_array(rgb, a, hex_color, mode)
    res = np.dstack([out, a])
    Image.fromarray((res * 255 + 0.5).astype(np.uint8), "RGBA").save(dst, "WEBP", lossless=True)
    return dst


# ════════════════════════════════════════════════════════════════
#  LOTTIE / TGS
# ════════════════════════════════════════════════════════════════

def _is_color(k) -> bool:
    return (isinstance(k, list) and len(k) >= 3
            and all(isinstance(x, (int, float)) for x in k[:3]))


def _walk(node, fn):
    """Вызывает fn(rgb_list) -> новый rgb_list для каждого цвета Lottie (заливки, обводки, градиенты)."""
    if isinstance(node, dict):
        # сплошной цвет слоя
        if node.get("ty") == 1 and isinstance(node.get("sc"), str) and node["sc"].startswith("#") and len(node["sc"]) == 7:
            r, g, b = fn(list(_hex_rgb(node["sc"])))
            node["sc"] = "#{:02x}{:02x}{:02x}".format(*(int(round(c * 255)) for c in (r, g, b)))
        for key, val in list(node.items()):
            if key == "c" and isinstance(val, dict) and "k" in val and node.get("ty") in ("fl", "st", None, "gf", "gs"):
                k = val["k"]
                if _is_color(k):
                    new = fn(k[:3])
                    val["k"] = list(new) + k[3:]
                elif isinstance(k, list):
                    for kf in k:
                        if isinstance(kf, dict):
                            for f in ("s", "e"):
                                if _is_color(kf.get(f)):
                                    new = fn(kf[f][:3])
                                    kf[f] = list(new) + kf[f][3:]
            elif key == "g" and isinstance(val, dict) and isinstance(val.get("k"), dict):
                _gradient(val, fn)
            else:
                _walk(val, fn)
    elif isinstance(node, list):
        for item in node:
            _walk(item, fn)


def _gradient(g: dict, fn):
    """Градиент: p цветовых остановок по 4 числа (позиция, r, g, b), дальше возможные остановки альфы."""
    holder = g["k"]
    arrs = []
    if isinstance(holder.get("k"), list) and holder["k"] and isinstance(holder["k"][0], (int, float)):
        arrs.append(holder["k"])
    elif isinstance(holder.get("k"), list):
        for kf in holder["k"]:
            if isinstance(kf, dict):
                for f in ("s", "e"):
                    if isinstance(kf.get(f), list):
                        arrs.append(kf[f])
    n = int(g.get("p", 0) or 0)
    for arr in arrs:
        for i in range(n):
            j = i * 4 + 1
            if j + 2 < len(arr):
                arr[j], arr[j + 1], arr[j + 2] = fn([arr[j], arr[j + 1], arr[j + 2]])


def recolor_tgs(src: str, dst: str, hex_color: str, mode: str) -> int:
    doc = vector.load_tgs(src)
    seen = []

    def collect(c):
        seen.append(c[:3])
        return c

    _walk(doc, collect)
    if not seen:
        return vector.write_tgs(doc, dst)

    pal = np.array(seen, np.float32)
    w = np.ones(len(pal), np.float32)
    lo, hi = _lum(pal).min(), _lum(pal).max()
    target = _hex_rgb(hex_color)

    dom = None
    use_hue = False
    if mode == "hue":
        th, ts, _ = colorsys.rgb_to_hsv(*target)
        dom = _dominant_hue(pal, w)
        use_hue = ts >= 0.12 and dom is not None
        t_h = th

    def transform(c):
        rgb = np.array([c[:3]], np.float32)
        if use_hue:
            h, s, v = _rgb_to_hsv(rgb)
            out = _hsv_to_rgb((h + (t_h - dom)) % 1.0, s, v)[0]
        else:
            lum = float(_lum(rgb)[0])
            norm = min(1.0, max(0.0, (lum - lo) / (hi - lo))) if hi > lo else 1.0
            out = np.clip((FLOOR + (1 - FLOOR) * norm) * np.array(target, np.float32), 0, 1)
        return [round(float(x), 4) for x in out]

    _walk(doc, transform)
    return vector.write_tgs(doc, dst)


def recolor_tgs_fit(src: str, dst: str, hex_color: str, mode: str) -> int:
    """Перекраска с гарантией лимита 64 КБ: если файл вырос, ужимаем."""
    tmp = dst + ".raw"
    try:
        recolor_tgs(src, tmp, hex_color, mode)
        if os.path.getsize(tmp) <= vector.TGS_LIMIT:
            os.replace(tmp, dst)
            return os.path.getsize(dst)
        return vector.optimize_tgs(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ════════════════════════════════════════════════════════════════
#  ВИДЕО (webm)
# ════════════════════════════════════════════════════════════════

def _decode_webm(src: str, size: int = 100):
    """webm -> (кадры RGBA [N,H,W,4], fps). Декодер libvpx обязателен: родной ffmpeg теряет альфу."""
    probe = subprocess.run([media.ffmpeg_path, "-hide_banner", "-c:v", "libvpx-vp9", "-i", src],
                           capture_output=True).stderr.decode(errors="replace")
    fps = 30.0
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", probe)
    if m:
        fps = min(30.0, float(m.group(1)))
    vf = (f"fps={fps},scale={size}:{size}:force_original_aspect_ratio=decrease,"
          f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,format=rgba")
    cmd = [media.ffmpeg_path, "-v", "error", "-c:v", "libvpx-vp9", "-i", src, "-t", "3",
           "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgba", "-"]
    res = subprocess.run(cmd, capture_output=True)
    raw = res.stdout
    frame_bytes = size * size * 4
    n = len(raw) // frame_bytes
    if n == 0:
        # запасной путь без libvpx-декодера (альфы не будет, но файл хотя бы перекрасим)
        cmd = [media.ffmpeg_path, "-v", "error", "-i", src, "-t", "3", "-vf", vf,
               "-f", "rawvideo", "-pix_fmt", "rgba", "-"]
        raw = subprocess.run(cmd, capture_output=True).stdout
        n = len(raw) // frame_bytes
    if n == 0:
        raise RuntimeError("не удалось прочитать кадры видео")
    return np.frombuffer(raw[: n * frame_bytes], np.uint8).reshape(n, size, size, 4).copy(), fps


def recolor_webm(src: str, dst: str, hex_color: str, mode: str) -> bool:
    frames, fps = _decode_webm(src)
    f = frames.astype(np.float32) / 255
    rgb, a = f[..., :3], f[..., 3]
    target = _hex_rgb(hex_color)

    vis = a > 0.02
    if mode == "hue":
        th, ts, _ = colorsys.rgb_to_hsv(*target)
        dom = _dominant_hue(rgb.reshape(-1, 3), a.reshape(-1)) if ts >= 0.12 else None
    else:
        dom = None

    if dom is not None:
        h, s, v = _rgb_to_hsv(rgb)
        out = _hsv_to_rgb((h + (th - dom)) % 1.0, s, v)
    else:
        lum = _lum(rgb)
        lo, hi = (lum[vis].min(), lum[vis].max()) if vis.any() else (0.0, 1.0)   # единая нормировка на весь ролик
        norm = np.clip((lum - lo) / (hi - lo), 0, 1) if hi > lo else np.ones_like(lum)
        out = np.clip((FLOOR + (1 - FLOOR) * norm)[..., None] * np.array(target, np.float32), 0, 1)

    res = np.concatenate([out, a[..., None]], axis=-1)
    res = (res * 255 + 0.5).astype(np.uint8)
    return media._frames_to_webm(res, fps, dst)
