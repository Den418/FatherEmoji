import asyncio
import gzip
import logging
import os
import subprocess
import tempfile

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

    no_alpha: источник в принципе не умеет в альфа-канал (mp4/gif/avi и т.д.) —
              кодируем как yuv420p, без прозрачности (легче и быстрее).
    force_drop_alpha: источник МОГ иметь альфу (настоящий webm), но это последний,
              самый агрессивный уровень сжатия — жертвуем прозрачностью ради того,
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
#  ВЕКТОРНЫЕ ЭМОДЗИ (растр → SVG → Lottie → .tgs 512×512)
# ════════════════════════════════════════════════════════════════

class VectorizeError(Exception):
    pass

class VectorizerNotInstalled(VectorizeError):
    pass

class ImageTooComplex(VectorizeError):
    pass


# Профили векторизации: от максимального качества к агрессивному сжатию.
# Лимит .tgs жёсткий (64 КБ), поэтому начинаем с максимума и спускаемся,
# пока файл не влезет.
VECT_PROFILES = [
    dict(
        filter_speckle=4,  color_precision=8, layer_difference=8,  path_precision=6,
        corner_threshold=30, length_threshold=3.5, max_iterations=30, splice_threshold=30,
    ),
    dict(
        filter_speckle=10, color_precision=4, layer_difference=16, path_precision=3,
        corner_threshold=60, length_threshold=4.0, max_iterations=10, splice_threshold=45,
    ),
    dict(
        filter_speckle=20, color_precision=3, layer_difference=32, path_precision=2,
        corner_threshold=90, length_threshold=5.0, max_iterations=8,  splice_threshold=60,
    ),
]


def _vectorize_sync(src: str, dst: str) -> str:
    # vtracer и lottie — необязательные тяжёлые зависимости, подключаем лениво:
    # без них бот работает, просто векторный режим честно сообщает, что не установлен.
    try:
        import vtracer
        from lottie.exporters.core import export_lottie
        from lottie.parsers.svg.importer import parse_svg_file
    except ImportError as e:
        raise VectorizerNotInstalled(str(e))

    with tempfile.TemporaryDirectory() as tmp:
        # 1. Ресайз в квадрат 512×512 с прозрачными полями — рабочий размер tgs.
        resized_png = os.path.join(tmp, "resized.png")
        with Image.open(src) as img:
            img = img.convert("RGBA")
            img.thumbnail((512, 512), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            canvas.paste(img, ((512 - img.width) // 2, (512 - img.height) // 2))
            canvas.save(resized_png, "PNG")

        # 2. Лестница качества: пробуем профили, пока gzip-результат не влезет в 64 КБ.
        for i, profile in enumerate(VECT_PROFILES):
            svg_path  = os.path.join(tmp, f"v{i}.svg")
            json_path = os.path.join(tmp, f"a{i}.json")
            try:
                vtracer.convert_image_to_svg_py(
                    resized_png, svg_path,
                    colormode='color', hierarchical='stacked', mode='spline',
                    **profile,
                )
                animation = parse_svg_file(svg_path)
                animation.width = 512
                animation.height = 512
                animation.frame_rate = 30
                animation.out_point = 30
                export_lottie(animation, json_path)
            except Exception as e:
                log.warning(f"Профиль векторизации #{i} не прошёл: {e}")
                continue

            with open(json_path, "rb") as f_in, gzip.open(dst, "wb") as f_out:
                f_out.write(f_in.read())

            if os.path.getsize(dst) <= TGS_LIMIT:
                return dst
            os.remove(dst)

    raise ImageTooComplex()


async def vectorize_image(src: str, dst: str) -> str:
    """Картинка → векторный .tgs. Возвращает путь к файлу, кидает VectorizeError."""
    return await asyncio.to_thread(_vectorize_sync, src, dst)


# ════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ТИПА ФАЙЛА
# ════════════════════════════════════════════════════════════════

def detect_type(path: str) -> str:
    """
    Определяем тип строго по магическим байтам содержимого, а не по типу
    сообщения в Telegram — метаданные (photo/video/animation/document) не
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
        return "video"     # mp4/mov/m4v — так реально приходят гифки/видео из Telegram
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return "video"     # настоящий .gif, присланный файлом-документом
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return "video"     # изредка присылают .avi документом
    return "image"         # всё остальное отдаём Pillow (png/jpg/webp/bmp...)
