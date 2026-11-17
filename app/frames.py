"""Кадры рефа для зрителя-агента: сетка кадров с разметкой в процентах.

Каждый кадр кадрируется так же, как реф ляжет в ролик (cover под WxH), поэтому проценты
от кадра на листе = проценты от финального кадра 1080x1920. Агент называет зоны в процентах.
"""
import base64
import io
import os
import subprocess
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

FONTS_DIR = os.getenv("FONTS_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts"))


class FramesRequest(BaseModel):
    url: str
    count: int = 8                     # сколько кадров равномерно по ролику
    times: Optional[List[float]] = None  # или конкретные секунды
    columns: int = 4
    cell_width: int = 360
    width: int = 1080                  # под какой кадр кадрировать (как в рендере)
    height: int = 1920
    grid: bool = True                  # сетка 10% с подписями


def _font(size: int):
    try:
        return ImageFont.truetype(os.path.join(FONTS_DIR, "Montserrat-Bold.ttf"), size)
    except OSError:
        return ImageFont.load_default()


def grab(src: str, t: float, w: int, h: int) -> Image.Image:
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-vf", vf,
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        return Image.new("RGB", (w, h), (60, 60, 60))
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def build_frames(req: FramesRequest, src: str, duration: float) -> dict:
    cw = max(160, min(540, req.cell_width))
    chh = int(round(cw * req.height / req.width))
    if req.times:
        times = [max(0.0, min(duration - 0.05, float(t))) for t in req.times][:16]
    else:
        n = max(1, min(16, req.count))
        a, b = duration * 0.03, duration * 0.97
        times = [round(a + (b - a) * (i + 0.5) / n, 2) for i in range(n)]
    cols = max(1, min(len(times), req.columns))
    rows = (len(times) + cols - 1) // cols
    head, ml, gap = 34, 30, 14                  # шапка с номером, поле под подписи %, промежуток
    W = cols * (ml + cw) + (cols + 1) * gap
    H = rows * (head + chh + 22) + (rows + 1) * gap
    sheet = Image.new("RGB", (W, H), (18, 18, 18))
    d = ImageDraw.Draw(sheet)
    f_head, f_lab = _font(20), _font(13)
    for i, t in enumerate(times):
        r, c = divmod(i, cols)
        x0 = gap + c * (ml + cw + gap) + ml
        y0 = gap + r * (head + chh + 22 + gap)
        d.text((x0, y0 + 6), f"Кадр {i + 1} · {t:.1f} с", fill=(255, 212, 0), font=f_head)
        im = grab(src, t, cw, chh)
        if req.grid:
            ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            for p in range(10, 100, 10):
                yy = int(chh * p / 100)
                xx = int(cw * p / 100)
                a = 170 if p == 50 else 95
                od.line([(0, yy), (cw, yy)], fill=(0, 255, 255, a), width=1)
                od.line([(xx, 0), (xx, chh)], fill=(0, 255, 255, a), width=1)
            im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
        sheet.paste(im, (x0, y0 + head))
        if req.grid:
            for p in range(0, 101, 10):
                yy = y0 + head + int(chh * p / 100)
                d.text((x0 - ml + 2, yy - 7), str(p), fill=(0, 230, 230), font=f_lab)
            for p in range(0, 101, 20):
                xx = x0 + int(cw * p / 100)
                d.text((xx - 8, y0 + head + chh + 3), str(p), fill=(0, 230, 230), font=f_lab)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=85)
    data = buf.getvalue()
    return {"data": data, "times": times, "columns": cols, "cell": [cw, chh]}


def to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
