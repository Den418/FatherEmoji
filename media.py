import asyncio
import gzip
import logging
import os
import subprocess

import imageio_ffmpeg
from PIL import Image, UnidentifiedImageError

log = logging.getLogger("fatheremoji")

ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

TGS_LIMIT = 64 * 1024  # жёсткий лимит .tgs у Telegram


# ════════════════════════════════════════════════════════════════
#  СТАТИЧНЫЕ ЭМОДЗИ (webp 100×100)
# ════════════════════════════════════════════════════════════════

async def resize_image(src: str, dst: str) -> bool:
    def _resize():
        try:
            img = Image.open(src).convert("RGBA")
            img.thumbnail((100, 100), Image.Resampling.LANCZOS)
            bg = Image.new('RGBA', (100, 100), (255, 255, 255, 0))
            loc = ((100 - img.width) // 2, (100 - img.height) // 2)
            bg.paste(img, loc)
            bg.save(dst, "WEBP", quality=100, lossless=True)
            return True
        except UnidentifiedImageError:
            return False
        except Exception as e:
            log.error(f"PIL resize error: {e}")
            return False
    return await asyncio.to_thread(_resize)


# ════════════════════════════════════════════════════════════════
#  ВИДЕО-ЭМОДЗИ (webm VP9 ≤245 КБ, 100×100)
# ════════════════════════════════════════════════════════════════

async def convert_to_webm(
    src: str, dst: str,
    fallback_bitrate: bool = False,
    no_alpha: bool = False,
    max_duration: float = 2.9,
    force_drop_alpha: bool = False,
) -> bool:
    """
    Конвертирует видео/гифку/видео-стикер в WEBM-стикер (≤245 КБ, 100×100).

    no_alpha: источник в принципе не умеет в альфа-канал (mp4/gif/avi и т.д.),
              кодируем как yuv420p, без прозрачности (легче и быстрее).
    force_drop_alpha: источник МОГ иметь альфу (настоящий webm), но это последний,
              самый агрессивный уровень сжатия - жертвуем прозрачностью ради того,
              чтобы гарантированно уложиться в лимит размера.
    max_duration: обрезает длинные файлы (третий+ уровень сжатия = 1.5 сек).
    """
    def _conv():
        bitrate_v = "80k"  if fallback_bitrate else "350k"
        maxrate   = "100k" if fallback_bitrate else "450k"
        bufsize   = "160k" if fallback_bitrate else "900k"
        fps       = "15"   if fallback_bitrate else "24"

        scale_pad = (
            "scale=100:100:force_original_aspect_ratio=decrease,"
            "pad=100:100:(ow-iw)/2:(oh-ih)/2"
        )

        drop_alpha = no_alpha or force_drop_alpha

        if drop_alpha:
            cmd = [
                ffmpeg_path, "-y",
                "-i", src,
                "-t", str(max_duration), "-an",
                "-vf", f"fps={fps},{scale_pad}",
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p",
                "-b:v", bitrate_v, "-maxrate", maxrate, "-bufsize", bufsize,
                "-fs", "245760",
                dst,
            ]
        else:
            cmd = [
                ffmpeg_path, "-y",
                "-i", src,
                "-t", str(max_duration), "-an",
                "-vf", f"fps={fps},{scale_pad}:color=#00000000",
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-b:v", bitrate_v, "-maxrate", maxrate, "-bufsize", bufsize,
                "-fs", "245760",
                dst,
            ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            log.warning(f"ffmpeg [{max_duration}s drop_alpha={drop_alpha}] stderr: {result.stderr.decode(errors='replace')[-400:]}")
        return os.path.exists(dst) and 0 < os.path.getsize(dst) <= 245760

    return await asyncio.to_thread(_conv)


# ════════════════════════════════════════════════════════════════
#  ВЕКТОРНЫЕ ЭМОДЗИ (растр -> .tgs 512x512). Сама логика живёт в vector.py
# ════════════════════════════════════════════════════════════════

from vector import (  # noqa: E402,F401
    ImageTooComplex, RenderUnavailable, TGS_LIMIT, VectorizeError, VectorizerNotInstalled,
)
import vector  # noqa: E402


async def vectorize_image(src: str, dst: str) -> str:
    """Картинка -> векторный .tgs. Возвращает путь к файлу, кидает VectorizeError."""
    return await asyncio.to_thread(vector.vectorize_sync, src, dst)


# ════════════════════════════════════════════════════════════════
#  ТЯЖЁЛЫЕ СТИКЕРЫ: ужимаем под требования эмодзи вместо отказа
# ════════════════════════════════════════════════════════════════

WEBM_LIMIT = 256 * 1000     # у Telegram для видео-эмодзи 256 КБ; берём с запасом ниже


def _frames_to_webm(frames, fps: float, dst: str, size: int = 100) -> bool:
    """Кадры RGBA (N,H,W,4) -> webm VP9 с прозрачностью. Лесенка: качество, потом fps и длина."""
    import numpy as np  # локально, чтобы модуль грузился и без numpy в редких окружениях

    ladder = [
        dict(crf=34, fps=fps, max_s=2.95),
        dict(crf=40, fps=min(fps, 24), max_s=2.95),
        dict(crf=46, fps=min(fps, 20), max_s=2.5),
        dict(crf=52, fps=min(fps, 15), max_s=2.0),
        dict(crf=58, fps=min(fps, 12), max_s=1.5),
    ]
    n_src = len(frames)
    for step in ladder:
        take = min(n_src, int(step["max_s"] * fps))
        idx = np.linspace(0, n_src - 1, max(1, round(take * step["fps"] / fps))).astype(int) if step["fps"] < fps else np.arange(take)
        clip = frames[idx]
        cmd = [
            ffmpeg_path, "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
            "-s", f"{size}x{size}", "-r", f"{step['fps']:.3f}", "-i", "-",
            "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-b:v", "0", "-crf", str(step["crf"]), "-auto-alt-ref", "0",
            "-deadline", "good", "-cpu-used", "4", dst,
        ]
        res = subprocess.run(cmd, input=clip.tobytes(), capture_output=True)
        if res.returncode == 0 and os.path.exists(dst) and 0 < os.path.getsize(dst) <= WEBM_LIMIT:
            return True
    return False


def _tgs_to_webm_sync(src: str, dst: str) -> bool:
    frames, fps = vector.render_tgs(src, size=100, max_frames=90, max_fps=30)
    return _frames_to_webm(frames, fps, dst)


async def shrink_tgs(src: str, dst: str) -> str:
    """
    Тяжёлый .tgs -> что-то, что примет пак анимированных эмодзи.
    Возвращает "tgs" (файл dst готов, влез в 64 КБ после сжатия) или бросает ImageTooComplex.
    Конвертация в webm здесь не делается: формат пака смешивать нельзя.
    """
    return await asyncio.to_thread(_shrink_tgs_sync, src, dst)


def _shrink_tgs_sync(src: str, dst: str) -> str:
    vector.optimize_tgs(src, dst)
    return "tgs"


async def tgs_to_webm(src: str, dst: str) -> bool:
    """Анимацию .tgs -> видео-эмодзи (для видео-паков, где .tgs не примут)."""
    return await asyncio.to_thread(_tgs_to_webm_sync, src, dst)


async def tgs_to_static(src: str, dst: str) -> bool:
    """Первый осмысленный кадр .tgs -> статичный webp 100x100."""
    def _run():
        try:
            im = vector.tgs_poster(src, 100)
            im.save(dst, "WEBP", quality=100, lossless=True)
            return True
        except Exception as e:
            log.warning(f"tgs_to_static: {e}")
            return False
    return await asyncio.to_thread(_run)


async def video_to_static(src: str, dst: str) -> bool:
    """Видео/webm -> статичный webp 100x100 (кадр из начала, с альфой, если есть)."""
    def _run():
        png = dst + ".png"
        cmd = [ffmpeg_path, "-y", "-c:v", "libvpx-vp9", "-i", src, "-frames:v", "1",
               "-vf", "scale=100:100:force_original_aspect_ratio=decrease,"
                      "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000,format=rgba", png]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not os.path.exists(png):
            cmd = [ffmpeg_path, "-y", "-i", src, "-frames:v", "1",
                   "-vf", "scale=100:100:force_original_aspect_ratio=decrease,"
                          "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000,format=rgba", png]
            r = subprocess.run(cmd, capture_output=True)
        if not os.path.exists(png):
            return False
        try:
            Image.open(png).convert("RGBA").save(dst, "WEBP", quality=100, lossless=True)
            return True
        finally:
            os.remove(png)
    return await asyncio.to_thread(_run)


async def compress_video(raw: str, out: str, no_alpha: bool) -> bool:
    """Лесенка сжатия видео под 100x100 и 245 КБ: от качества к жёсткой экономии."""
    ok = await convert_to_webm(raw, out, no_alpha=no_alpha)
    if not ok:
        ok = await convert_to_webm(raw, out, fallback_bitrate=True, no_alpha=no_alpha)
    if not ok:
        ok = await convert_to_webm(raw, out, fallback_bitrate=True, no_alpha=no_alpha, max_duration=1.5)
    if not ok and not no_alpha:
        ok = await convert_to_webm(raw, out, fallback_bitrate=True, no_alpha=no_alpha,
                                   max_duration=1.5, force_drop_alpha=True)
    return ok


async def image_to_webm(src: str, dst: str) -> bool:
    """Картинка -> короткое видео-эмодзи (для видео-паков)."""
    def _run():
        import numpy as np
        img = Image.open(src).convert("RGBA")
        img.thumbnail((100, 100), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        canvas.paste(img, ((100 - img.width) // 2, (100 - img.height) // 2))
        frames = np.repeat(np.asarray(canvas)[None], 30, axis=0)
        return _frames_to_webm(frames, 30.0, dst)
    return await asyncio.to_thread(_run)


class AdaptError(Exception):
    """Файл нельзя привести к нужному формату. reason: короткий код для сообщения пользователю."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


NATIVE = {"image": "static", "tgs": "animated", "webm": "video", "video": "video"}
_MIME = {"static": ("image/webp", "ready.webp"), "animated": ("application/x-tgsticker", "ready.tgs"),
         "video": ("video/webm", "ready.webm")}


async def adapt(raw: str, src_kind: str, target: str | None, tmp: str) -> tuple[str, str, str, str, str]:
    """
    Приводит файл к формату пака: static (webp 100x100), animated (.tgs <= 64 КБ), video (webm <= 245 КБ).
    target=None: пак пуст, формат родной. Возвращает (путь, формат, mime, имя файла).
    Вектор, видео и статика взаимно конвертируются, поэтому "тяжёлый" или "чужой" формат
    больше не повод для отказа. Пятый элемент результата: "" или "frame", если
    анимацию пришлось упростить до одного кадра.
    """
    target = target or NATIVE[src_kind]
    out = os.path.join(tmp, _MIME[target][1])
    mime, name = _MIME[target]
    note = ""

    if target == "static":
        if src_kind == "image":
            ok = await resize_image(raw, out)
        elif src_kind == "tgs":
            ok = await tgs_to_static(raw, out)
        else:
            ok = await video_to_static(raw, out)
        if not ok:
            raise AdaptError("bad_file")

    elif target == "animated":
        try:
            if src_kind == "tgs":
                if os.path.getsize(raw) <= TGS_LIMIT:
                    shutil_copy(raw, out)
                else:
                    try:
                        await shrink_tgs(raw, out)
                    except ImageTooComplex:
                        # Совсем тяжёлая анимация: оставляем один кадр вектором, лишь бы эмодзи доехал.
                        frame = os.path.join(tmp, "frame.webp")
                        if not await tgs_to_static(raw, frame):
                            raise
                        await vectorize_image(frame, out)
                        note = "frame"
            else:
                frame = raw
                if src_kind in ("webm", "video"):
                    frame = os.path.join(tmp, "frame.webp")
                    if not await video_to_static(raw, frame):
                        raise AdaptError("bad_file")
                await vectorize_image(frame, out)
        except ImageTooComplex:
            raise AdaptError("too_complex")
        except VectorizerNotInstalled:
            raise AdaptError("no_vector")

    else:  # video
        if src_kind in ("webm", "video"):
            ok = await compress_video(raw, out, no_alpha=(src_kind == "video"))
        elif src_kind == "tgs":
            try:
                ok = await tgs_to_webm(raw, out)
            except RenderUnavailable:
                raise AdaptError("no_render")
        else:
            ok = await image_to_webm(raw, out)
        if not ok:
            raise AdaptError("too_heavy")

    return out, target, mime, name, note


def shutil_copy(src: str, dst: str):
    import shutil
    shutil.copyfile(src, dst)


# ════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ТИПА ФАЙЛА
# ════════════════════════════════════════════════════════════════

def detect_type(path: str) -> str:
    """
    Определяем тип строго по магическим байтам содержимого, а не по типу
    сообщения в Telegram - метаданные (photo/video/animation/document) не
    гарантируют реальный формат файла на диске.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return "empty"
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b'\x1f\x8b'):
        return "tgs"       # векторы сжаты GZIP
    if head.startswith(b'\x1aE\xdf\xa3'):
        return "webm"      # видео-стикер (Matroska/EBML), может нести альфа-канал
    if head[4:8] == b'ftyp':
        return "video"     # mp4/mov/m4v - так реально приходят гифки/видео из Telegram
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return "video"     # настоящий .gif, присланный файлом-документом
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return "video"     # изредка присылают .avi документом
    return "image"         # всё остальное отдаём Pillow (png/jpg/webp/bmp...)
