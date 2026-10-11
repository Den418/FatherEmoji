"""
Скачивание эмодзи: исходный файл и все удобные конвертации.

  статичный  webp (исходник), png, jpg (белый фон), gif
  анимация   tgs (исходник), json (Lottie), gif, apng, webp (анимированный), webm (с прозрачностью),
             mp4 (белый фон), png (кадр)
  видео      webm (исходник), gif, apng, webp (анимированный), mp4 (белый фон), png (кадр)

Всё делается из последовательности кадров с настоящей прозрачностью: .tgs рисуется через rlottie
в 512x512, .webm разбирается декодером libvpx, который сохраняет альфа-канал.
Формат, который не получился (например, нет нужного кодека), пропускается и попадает в список failed,
остальные файлы это не ломает.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

import media
import vector

MAX_FPS = 30
MAX_SECONDS = 3.1
TGS_SIZE = 512
FF_TIMEOUT = 180
ZIP_LIMIT = 45 * 1024 * 1024        # запас до лимита Telegram на файл


@dataclass
class Result:
    files: list = field(default_factory=list)     # пути в порядке показа, первым идёт исходник
    failed: list = field(default_factory=list)    # форматы, которые не получились
    notes: list = field(default_factory=list)


# ════════════════════════════════════════════════════════════════
#  КАДРЫ
# ════════════════════════════════════════════════════════════════

def _frames_from_tgs(src: str, dst: str, size: int = TGS_SIZE):
    """-> (число кадров, fps, (w, h)). Кадры лежат в dst как f_0000.png, ..."""
    from rlottie_python import LottieAnimation
    anim = LottieAnimation.from_tgs(src)
    total = int(anim.lottie_animation_get_totalframe())
    src_fps = float(anim.lottie_animation_get_framerate() or 30)
    fps = min(float(MAX_FPS), src_fps)
    step = src_fps / fps
    n = max(1, min(int(total / step), int(MAX_SECONDS * fps)))
    for i in range(n):
        im = vector._render_frame(anim, min(total - 1, int(round(i * step))), size, supersample=1)
        im.save(os.path.join(dst, f"f_{i:04d}.png"), compress_level=1)
    return n, fps, (size, size)


def _probe_video(src: str):
    r = subprocess.run([media.ffmpeg_path, "-hide_banner", "-c:v", "libvpx-vp9", "-i", src],
                       capture_output=True, timeout=60)
    text = r.stderr.decode(errors="replace")
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (100, 100)
    f = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    fps = min(float(MAX_FPS), float(f.group(1))) if f else 30.0
    return w, h, fps


def _frames_from_webm(src: str, dst: str):
    w, h, fps = _probe_video(src)
    base = [media.ffmpeg_path, "-v", "error"]
    tail = ["-t", f"{MAX_SECONDS}", "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "rgba", "-"]
    raw = subprocess.run(base + ["-c:v", "libvpx-vp9", "-i", src] + tail, capture_output=True, timeout=FF_TIMEOUT).stdout
    size = w * h * 4
    if len(raw) < size:       # декодера libvpx нет: родной декодер отдаст кадры без прозрачности
        raw = subprocess.run(base + ["-i", src] + tail, capture_output=True, timeout=FF_TIMEOUT).stdout
    n = len(raw) // size
    if n == 0:
        raise RuntimeError("не удалось прочитать кадры видео")
    arr = np.frombuffer(raw[: n * size], np.uint8).reshape(n, h, w, 4)
    for i in range(n):
        Image.fromarray(arr[i], "RGBA").save(os.path.join(dst, f"f_{i:04d}.png"), compress_level=1)
    return n, fps, (w, h)


# ════════════════════════════════════════════════════════════════
#  КОДИРОВЩИКИ (из папки с кадрами)
# ════════════════════════════════════════════════════════════════

def _ff(cmd: list) -> bool:
    try:
        r = subprocess.run([media.ffmpeg_path, "-y", "-v", "error"] + cmd, capture_output=True, timeout=FF_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def _inp(frames: str, fps: float) -> list:
    return ["-framerate", f"{fps:g}", "-i", os.path.join(frames, "f_%04d.png")]


def _ok(out: str) -> bool:
    return os.path.exists(out) and os.path.getsize(out) > 0


def _enc_gif(frames: str, fps: float, out: str) -> bool:
    vf = ("split[a][b];[a]palettegen=reserve_transparent=1:stats_mode=full[p];"
          "[b][p]paletteuse=alpha_threshold=128:dither=bayer:bayer_scale=4")
    return _ff(_inp(frames, fps) + ["-vf", vf, "-loop", "0", out]) and _ok(out)


def _enc_apng(frames: str, fps: float, out: str) -> bool:
    return _ff(_inp(frames, fps) + ["-plays", "0", "-f", "apng", out]) and _ok(out)


def _enc_webp(frames: str, fps: float, out: str) -> bool:
    return _ff(_inp(frames, fps) + ["-c:v", "libwebp_anim", "-lossless", "0", "-q:v", "88", "-loop", "0", out]) and _ok(out)


def _enc_webm(frames: str, fps: float, out: str) -> bool:
    return _ff(_inp(frames, fps) + ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30",
                                      "-auto-alt-ref", "0", "-deadline", "good", "-cpu-used", "4", out]) and _ok(out)


def _enc_mp4(frames: str, fps: float, out: str, size: tuple) -> bool:
    """MP4 не умеет прозрачность: рисуем на белом фоне. Нет libx264: пробуем mpeg4."""
    w, h = size
    graph = "[0:v][1:v]overlay=shortest=1,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    src = ["-f", "lavfi", "-i", f"color=c=white:s={w}x{h}:r={fps:g}"] + _inp(frames, fps)
    for codec in (["-c:v", "libx264", "-crf", "18", "-preset", "medium"], ["-c:v", "mpeg4", "-q:v", "2"]):
        if _ff(src + ["-filter_complex", graph] + codec + ["-movflags", "+faststart", out]) and _ok(out):
            return True
    return False


# ════════════════════════════════════════════════════════════════
#  СТАТИЧНЫЕ КАРТИНКИ (Pillow)
# ════════════════════════════════════════════════════════════════

def _png(im: Image.Image, out: str) -> bool:
    im.save(out, "PNG", optimize=True)
    return _ok(out)


def _jpg(im: Image.Image, out: str, bg=(255, 255, 255)) -> bool:
    base = Image.new("RGB", im.size, bg)
    base.paste(im, mask=im.getchannel("A"))
    base.save(out, "JPEG", quality=95, subsampling=0)
    return _ok(out)


def _gif_still(im: Image.Image, out: str) -> bool:
    """Одиночный GIF с прозрачностью (1 бит: пиксель либо виден, либо нет)."""
    alpha = im.getchannel("A")
    pal = im.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=255)
    pal.paste(255, alpha.point(lambda a: 255 if a < 128 else 0))
    pal.save(out, "GIF", transparency=255, optimize=True)
    return _ok(out)


# ════════════════════════════════════════════════════════════════
#  ГЛАВНАЯ ФУНКЦИЯ
# ════════════════════════════════════════════════════════════════

def _first_frame_webm(src: str, out: str) -> bool:
    """Первый кадр видео с прозрачностью -> PNG."""
    w, h, _ = _probe_video(src)
    for dec in (["-c:v", "libvpx-vp9"], []):
        raw = subprocess.run([media.ffmpeg_path, "-v", "error"] + dec + ["-i", src, "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                             capture_output=True, timeout=FF_TIMEOUT).stdout
        if len(raw) >= w * h * 4:
            Image.frombuffer("RGBA", (w, h), raw[: w * h * 4], "raw", "RGBA", 0, 1).save(out, "PNG", optimize=True)
            return _ok(out)
    return False


# Что входит в каждый режим. None = всё.
ONLY_MODES = ("orig", "png", "gif")


def export_emoji(src: str, kind: str, out_dir: str, base: str, only: str | None = None) -> Result:
    """
    src: скачанный файл эмодзи, kind: image | tgs | webm (как отдаёт media.detect_type).
    Файлы складываются в out_dir с именами base.*, порядок в Result.files: исходник, затем остальное.

    only: None - все форматы; "orig" - только исходник; "png" - только PNG (у анимации кадр-превью);
          "gif" - только GIF (у анимации анимированный).
    """
    if only is not None and only not in ONLY_MODES:
        raise ValueError(f"неизвестный режим: {only}")
    os.makedirs(out_dir, exist_ok=True)
    res = Result()

    def add(name: str, ok: bool, path: str):
        if ok and _ok(path):
            res.files.append(path)
        else:
            res.failed.append(name)
            if os.path.exists(path):
                os.remove(path)

    def p(suffix: str) -> str:
        return os.path.join(out_dir, base + suffix)

    single = only in ("png", "gif")          # режим из одного формата: имена без суффиксов-пояснений

    if kind == "image":
        if only in (None, "orig"):
            shutil.copyfile(src, p(".webp"))
            res.files.append(p(".webp"))
        if only == "orig":
            return res
        im = Image.open(src).convert("RGBA")
        todo = (("PNG", _png, ".png"), ("JPG", _jpg, ".jpg"), ("GIF", _gif_still, ".gif"))
        for name, fn, suffix in todo:
            if only is not None and name.lower() != only:
                continue
            try:
                add(name, fn(im, p(suffix)), p(suffix))
            except Exception:
                res.failed.append(name)
        return res

    if kind == "tgs":
        ext0 = ".tgs"
    elif kind in ("webm", "video"):
        ext0 = ".webm"
    else:
        raise ValueError(f"неизвестный тип файла: {kind}")

    if only in (None, "orig"):
        shutil.copyfile(src, p(ext0))
        res.files.append(p(ext0))
    if only == "orig":
        return res
    if only is None and kind == "tgs":
        try:
            with open(p(".json"), "w", encoding="utf-8") as f:
                json.dump(vector.load_tgs(src), f, ensure_ascii=False, separators=(",", ":"))
            add("JSON", True, p(".json"))
        except Exception:
            res.failed.append("JSON")

    # ── только PNG: кадр без разбора всей анимации ──
    if only == "png":
        try:
            if kind == "tgs":
                add("PNG", _png(vector.tgs_poster(src, TGS_SIZE), p(".png")), p(".png"))
            else:
                add("PNG", _first_frame_webm(src, p(".png")), p(".png"))
        except Exception:
            res.failed.append("PNG")
        return res

    # ── анимации: общая последовательность кадров ──
    frames = tempfile.mkdtemp(prefix="exp_")
    try:
        try:
            n, fps, size = (_frames_from_tgs(src, frames) if kind == "tgs" else _frames_from_webm(src, frames))
        except Exception:
            res.failed += ["GIF"] if only == "gif" else ["GIF", "APNG", "WebP", "MP4", "PNG"] + (["WebM"] if kind == "tgs" else [])
            return res

        if only == "gif":
            add("GIF", _enc_gif(frames, fps, p(".gif")), p(".gif"))
            return res

        jobs = [("GIF", ".gif", lambda o: _enc_gif(frames, fps, o)),
                ("APNG", ".apng", lambda o: _enc_apng(frames, fps, o)),
                ("WebP", "_animated.webp", lambda o: _enc_webp(frames, fps, o))]
        if kind == "tgs":
            jobs.append(("WebM", "_alpha.webm", lambda o: _enc_webm(frames, fps, o)))
        jobs.append(("MP4", ".mp4", lambda o: _enc_mp4(frames, fps, o, size)))
        for name, suffix, fn in jobs:
            try:
                add(name, fn(p(suffix)), p(suffix))
            except Exception:
                res.failed.append(name)

        # кадр-превью: у .tgs берём самый «заполненный» кадр, у видео первый
        try:
            if kind == "tgs":
                im = vector.tgs_poster(src, TGS_SIZE)
            else:
                im = Image.open(os.path.join(frames, "f_0000.png")).convert("RGBA")
            add("PNG", _png(im, p("_frame.png")), p("_frame.png"))
        except Exception:
            res.failed.append("PNG")
    finally:
        shutil.rmtree(frames, ignore_errors=True)
    return res


# ════════════════════════════════════════════════════════════════
#  АРХИВЫ
# ════════════════════════════════════════════════════════════════

def make_zip(entries: list, out_path: str) -> str:
    """entries: [(имя внутри архива, путь к файлу)]."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for arc, path in entries:
            z.write(path, arc)
    return out_path


def make_zips(groups: list, out_dir: str, name: str) -> list:
    """
    groups: [(папка внутри архива, [файлы])] по одному на эмодзи.
    Складывает всё в один архив, а если он не влезает в лимит Telegram, делит на части по эмодзи.
    """
    parts, cur, size = [], [], 0
    for folder, files in groups:
        gsize = sum(os.path.getsize(f) for f in files)
        if cur and size + gsize > ZIP_LIMIT:
            parts.append(cur)
            cur, size = [], 0
        cur.append((folder, files))
        size += gsize
    if cur:
        parts.append(cur)

    out = []
    for i, part in enumerate(parts, 1):
        path = os.path.join(out_dir, f"{name}.zip" if len(parts) == 1 else f"{name}_{i}.zip")
        make_zip([(f"{folder}/{os.path.basename(f)}", f) for folder, files in part for f in files], path)
        out.append(path)
    return out


# Примерные секунды на один эмодзи (вместе со скачиванием) для режимов и форматов. Нужны только для первой оценки,
# потом время уточняется по замерам.
EST_SECONDS = {
    "all":  {"static": 0.8, "animated": 7.0, "video": 1.5},
    "orig": {"static": 0.5, "animated": 0.5, "video": 0.5},
    "png":  {"static": 0.6, "animated": 1.0, "video": 0.8},
    "gif":  {"static": 0.6, "animated": 3.5, "video": 1.0},
}


def estimate(mode: str, fmt: str, count: int) -> float:
    return count * EST_SECONDS[mode][fmt]


SPLIT_AT = 36 * 1024 * 1024         # закрываем часть архива, когда она доросла до этого размера (до лимита 45 МБ запас)


class ZipParts:
    """
    Собирает один большой архив пака по частям, не копя всё на диске: файлы одного эмодзи
    дописываются в текущую часть и сразу удаляются. Когда часть доросла до SPLIT_AT, открывается
    следующая. В каждую часть кладётся свой index.txt со списком её эмодзи.
    """

    def __init__(self, folder: str, name: str):
        self.folder, self.name = folder, name
        self.parts: list[list] = []          # [[путь, [строки индекса], число эмодзи]]
        self._z = None
        self._path = None
        self._index: list[str] = []
        self._count = 0

    def _open(self):
        self._path = os.path.join(self.folder, f"{self.name}_part{len(self.parts) + 1}.zip")
        self._z = zipfile.ZipFile(self._path, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
        self._index, self._count = [], 0

    def add(self, entries: list, index_line: str):
        """entries: [(имя в архиве, путь)] одного эмодзи."""
        if self._z is None:
            self._open()
        for arc, path in entries:
            self._z.write(path, arc)
        self._index.append(index_line)
        self._count += 1
        self._z.fp.flush()
        if os.path.getsize(self._path) >= SPLIT_AT:
            self._close()

    def _close(self):
        if self._z is None:
            return
        self._z.writestr("index.txt", "\n".join(self._index) + "\n")
        self._z.close()
        self.parts.append([self._path, self._index, self._count])
        self._z = None

    def finish(self) -> list[str]:
        """Закрывает последнюю часть. Если часть одна, у неё имя без суффикса _part1."""
        self._close()
        paths = []
        if len(self.parts) == 1:
            single = os.path.join(self.folder, f"{self.name}.zip")
            os.replace(self.parts[0][0], single)
            self.parts[0][0] = single
        paths = [p[0] for p in self.parts]
        return paths
