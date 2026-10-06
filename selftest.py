"""
Проверка окружения без запуска бота и без сети: python selftest.py
Показывает, чего не хватает на сервере (ffmpeg, vtracer, rlottie, шрифты).
"""
import asyncio
import os
import shutil
import sys
import tempfile

OK, BAD = "✅", "❌"
failed = []
OPTIONAL = {"Превью-ролик (libx264)"}


def check(name, fn):
    try:
        info = fn()
        print(f"{OK} {name}" + (f": {info}" if info else ""))
    except Exception as e:
        if name in OPTIONAL:
            print(f"⚠️ {name}: {e}")
            return
        failed.append(name)
        print(f"{BAD} {name}: {type(e).__name__}: {e}")


tmp = tempfile.mkdtemp()


def t_imports():
    import fontTools, numpy, PIL  # noqa: F401, E401
    return f"Python {sys.version.split()[0]}, Pillow {PIL.__version__}, numpy {numpy.__version__}"


def t_ffmpeg():
    import subprocess
    import media
    out = subprocess.run([media.ffmpeg_path, "-hide_banner", "-encoders"], capture_output=True).stdout.decode()
    assert "libvpx-vp9" in out, "в ffmpeg нет кодека libvpx-vp9 (нужен для видео-эмодзи)"
    return media.ffmpeg_path


def t_fonts():
    import textemoji
    from fontTools.ttLib import TTFont
    for key, (fname, _) in textemoji.FONTS.items():
        cm = TTFont(os.path.join(textemoji.FONT_DIR, fname)).getBestCmap()
        assert all(c in cm for c in list(range(0x410, 0x450)) + [0x401, 0x451]), f"{fname}: нет кириллицы"
    return f"{len(textemoji.FONTS)} шрифтов, кириллица есть везде"


def t_text():
    import textemoji
    st = textemoji.Style(text="Привет, мир", fmt="static")
    p, f, _ = textemoji.build(st, tmp)
    st.fmt = "vector"
    p2, f2, _ = textemoji.build(st, tmp)
    return f"webp {os.path.getsize(p)} Б, tgs {os.path.getsize(p2)} Б"


def t_effects():
    import textemoji
    base = dict(text="Привет", glow=2, glow_color="#3DFF9A", outline=1, shadow=True, grad=True, bg_grad=True)
    sizes = []
    for fmt, anim in (("static", "none"), ("vector", "wave"), ("video", "rainbow")):
        p, _, _ = textemoji.build(textemoji.Style(fmt=fmt, anim=anim, **base), tmp)
        sizes.append(f"{fmt} {os.path.getsize(p) // 1024} КБ")
    return ", ".join(sizes)


def t_preview():
    import textemoji
    st = textemoji.Style(text="Привет", fmt="video", anim="pulse")
    buf = textemoji.render_preview_anim(st)
    if buf is None:
        raise RuntimeError("в ffmpeg нет libx264: превью анимаций будет показываться картинкой, остальное работает")
    return f"mp4 {len(buf.getvalue()) // 1024} КБ"


def t_vector():
    import numpy as np
    from PIL import Image, ImageDraw
    import vector
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((40, 40, 470, 470), fill=(255, 200, 40, 255))
    d.ellipse((150, 160, 220, 240), fill=(255, 255, 255, 255))
    d.ellipse((300, 160, 370, 240), fill=(255, 255, 255, 255))
    d.ellipse((90, 90, 240, 160), fill=(255, 255, 255, 110))
    im.save(os.path.join(tmp, "v.png"))
    out = os.path.join(tmp, "v.tgs")
    vector.vectorize_sync(os.path.join(tmp, "v.png"), out)
    size = os.path.getsize(out)
    assert size <= vector.TGS_LIMIT
    return f"{size} Б из {vector.TGS_LIMIT}"


def t_render():
    import media
    import vector
    frames, fps = vector.render_tgs(os.path.join(tmp, "v.tgs"), size=100)
    ok = media._frames_to_webm(frames, fps, os.path.join(tmp, "v.webm"))
    assert ok, "не удалось собрать webm"
    return f"{len(frames)} кадров, webm {os.path.getsize(os.path.join(tmp, 'v.webm')) // 1024} КБ"


def t_recolor():
    import recolor
    recolor.recolor_tgs_fit(os.path.join(tmp, "v.tgs"), os.path.join(tmp, "r.tgs"), "#E5393B", "hue")
    assert recolor.recolor_webm(os.path.join(tmp, "v.webm"), os.path.join(tmp, "r.webm"), "#2F7BFF", "mono")
    return "tgs и webm"


def t_adapt():
    import media
    raw = os.path.join(tmp, "v.tgs")
    res = asyncio.run(media.adapt(raw, "tgs", "static", tmp))
    return f"tgs -> {res[1]}"


for title, fn in (("Библиотеки", t_imports), ("ffmpeg с VP9", t_ffmpeg), ("Шрифты", t_fonts),
                  ("Текстовые эмодзи", t_text), ("Эффекты и анимации (3 формата)", t_effects), ("Превью-ролик (libx264)", t_preview), ("Векторизация (vtracer)", t_vector),
                  ("Разбор .tgs (rlottie)", t_render), ("Перекраска", t_recolor), ("Конвертация формата", t_adapt)):
    check(title, fn)

shutil.rmtree(tmp, ignore_errors=True)
print()
if failed:
    print(f"{BAD} Проблемы: {', '.join(failed)}")
    sys.exit(1)
print(f"{OK} Всё на месте, бот можно запускать.")
