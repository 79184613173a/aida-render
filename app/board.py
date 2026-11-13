"""Раскадровка: сетка вертикальных кадров с подписями — одна картинка для Telegram."""
import io
import math
import os
from typing import List, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

FONTS_DIR = os.getenv("FONTS_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts"))


class Frame(BaseModel):
    url: Optional[str] = None       # нет картинки — серая заглушка
    label: str = ""                 # «1 · 0–3 с»
    tag: str = ""                   # «ВИДЕО» / «СТАТИКА»
    note: str = ""                  # короткая подпись под кадром


class BoardRequest(BaseModel):
    frames: List[Frame]
    title: str = ""
    columns: int = 0                # 0 = авто (3 до 6 кадров, иначе 4)
    width: int = 1440
    accent: str = "#FFD400"


def _font(name: str, size: int):
    try:
        return ImageFont.truetype(os.path.join(FONTS_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


def _fetch(client: httpx.Client, url: Optional[str]) -> Optional[Image.Image]:
    if not url:
        return None
    try:
        r = client.get(url)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> List[str]:
    words, lines, cur = (text or "").split(), [], ""
    for wd in words:
        test = (cur + " " + wd).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = wd
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and " ".join(lines) != text.strip():
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return lines


def build_board(req: BoardRequest) -> bytes:
    n = max(1, len(req.frames))
    cols = req.columns or (3 if n <= 6 else 4)
    pad = 16
    cell_w = (req.width - pad * (cols + 1)) // cols
    img_h = int(cell_w * 16 / 9)
    cap_h = 96
    title_h = 90 if req.title else pad
    rows = math.ceil(n / cols)
    H = title_h + rows * (img_h + cap_h + pad) + pad
    board = Image.new("RGB", (req.width, H), (18, 18, 20))
    draw = ImageDraw.Draw(board)

    f_title = _font("Montserrat-ExtraBold.ttf", 40)
    f_label = _font("Montserrat-Bold.ttf", 24)
    f_tag = _font("Montserrat-ExtraBold.ttf", 18)
    f_note = _font("Montserrat-Medium.ttf", 18)
    accent = req.accent

    if req.title:
        t = req.title if draw.textlength(req.title, font=f_title) < req.width - 2 * pad else req.title[:60] + "…"
        draw.text((pad + 4, 24), t, font=f_title, fill="#FFFFFF")

    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for i, fr in enumerate(req.frames):
            r, c = divmod(i, cols)
            x = pad + c * (cell_w + pad)
            y = title_h + r * (img_h + cap_h + pad)
            im = _fetch(client, fr.url)
            if im is not None:
                board.paste(_cover(im, cell_w, img_h), (x, y))
            else:
                draw.rectangle([x, y, x + cell_w, y + img_h], fill=(48, 48, 52))
                draw.text((x + cell_w // 2, y + img_h // 2), "нет кадра", font=f_label, fill="#888888", anchor="mm")
            # плашка-тег поверх кадра
            if fr.tag:
                tw = draw.textlength(fr.tag, font=f_tag)
                draw.rounded_rectangle([x + 10, y + 10, x + 26 + tw, y + 42], radius=8, fill=accent)
                draw.text((x + 18, y + 26), fr.tag, font=f_tag, fill="#111111", anchor="lm")
            # подпись под кадром
            cy = y + img_h + 10
            draw.text((x + 2, cy), fr.label, font=f_label, fill="#FFFFFF")
            for k, line in enumerate(_wrap(draw, fr.note, f_note, cell_w - 4, 2)):
                draw.text((x + 2, cy + 34 + k * 24), line, font=f_note, fill="#BDBDBD")

    out = io.BytesIO()
    board.save(out, "JPEG", quality=86, optimize=True)
    return out.getvalue()
