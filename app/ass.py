"""Субтитры и моушн-плашки в формате ASS (libass рисует их прямо в ffmpeg).

Типы плашек: title, plate, stat (цифра с отсчётом от нуля), list (пункты по очереди), cta.
"""
import re
from typing import List

from .models import Captions, RenderRequest, TextOverlay, TextStyle


def ass_color(hex_color: str, alpha: float = 0.0) -> str:
    """#RRGGBB -> &HAABBGGRR. alpha: 0 = непрозрачно, 1 = прозрачно."""
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    h = (h + "000000")[:6]
    r, g, b = h[0:2], h[2:4], h[4:6]
    a = max(0, min(255, int(round(alpha * 255))))
    return f"&H{a:02X}{b}{g}{r}".upper()


def inline_color(hex_color: str) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    h = (h + "000000")[:6]
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\U0001F1E6-\U0001F1FF]+")


def esc(text: str) -> str:
    """Экранирует ASS-разметку и выкидывает эмодзи: libass их не рисует, был бы квадрат."""
    text = re.sub(" {2,}", " ", EMOJI.sub("", text or "")).strip()
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def wrap(text: str, max_chars: int) -> str:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines)


class AssBuilder:
    def __init__(self, req: RenderRequest, total: float):
        self.W, self.H = req.width, req.height
        self.total = total
        self.k = self.W / 1080.0  # масштаб размеров относительно 1080 по ширине
        self.styles: List[str] = []
        self.events: List[str] = []
        self._n = 0

    # ---------- служебное ----------
    def style(self, st: TextStyle, size: int, align: int, margin_v: int, box: bool = None) -> str:
        """Регистрирует стиль, возвращает его имя."""
        self._n += 1
        name = f"S{self._n}"
        box = st.box if box is None else box
        border_style = 3 if box else 1
        back = ass_color(st.box_color, 1 - st.box_opacity) if box else ass_color("#000000", 0.6)
        outline_c = ass_color(st.box_color, 1 - st.box_opacity) if box else ass_color(st.outline)
        outline_w = 18 * self.k if box else st.outline_width * self.k
        self.styles.append(
            f"Style: {name},{st.font},{int(size * self.k)},{ass_color(st.color)},{ass_color(st.accent)},"
            f"{outline_c},{back},0,0,0,0,100,100,0,0,{border_style},{outline_w:.1f},0,{align},"
            f"{int(60 * self.k)},{int(60 * self.k)},{int(margin_v * self.k)},1"
        )
        return name

    def event(self, start: float, end: float, style: str, text: str, layer: int = 0):
        end = self.total if end is None or end < 0 else min(end, self.total)
        if end - start < 0.02:
            return
        self.events.append(f"Dialogue: {layer},{ts(start)},{ts(end)},{style},,0,0,0,,{text}")

    def pos(self, position: str):
        """(alignment, margin_v, y-центр в пикселях) для позиций плашек."""
        H = self.H
        table = {
            "top": (8, 260, 0.16),
            "upper": (5, 0, 0.30),
            "center": (5, 0, 0.45),
            "lower": (5, 0, 0.62),
            "bottom": (2, 560, 0.70),
        }
        a, mv, yc = table.get(position, table["center"])
        return a, mv, int(H * yc)

    # ---------- субтитры ----------
    def captions(self, cap: Captions):
        st = cap.style
        size = st.size or 86
        align, mv = {"bottom": (2, 520), "center": (5, 0), "top": (8, 300)}[cap.position]
        name = self.style(st, size, align, mv)
        accent = inline_color(st.accent)
        pop = "{\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}" if cap.pop else ""
        for seg in cap.segments:
            text = seg.text.upper() if st.uppercase else seg.text
            if cap.highlight and seg.words:
                words = [w.w.upper() if st.uppercase else w.w for w in seg.words]
                for i, w in enumerate(seg.words):
                    w_end = seg.words[i + 1].start if i + 1 < len(seg.words) else seg.end
                    parts = []
                    for j, token in enumerate(words):
                        t = esc(token)
                        parts.append(f"{{\\c{accent}}}{t}{{\\c{inline_color(st.color)}}}" if j == i else t)
                    line = self._wrap_tokens(parts, words, cap.max_chars)
                    lead = pop if i == 0 else ""
                    self.event(w.start, w_end, name, lead + line)
            else:
                self.event(seg.start, seg.end, name, pop + wrap(esc(text), cap.max_chars))

    @staticmethod
    def _wrap_tokens(parts, raw, max_chars):
        out, cur_len = "", 0
        for p, r in zip(parts, raw):
            if cur_len and cur_len + 1 + len(r) > max_chars:
                out += "\\N" + p
                cur_len = len(r)
            else:
                out += (" " if cur_len else "") + p
                cur_len += (1 if cur_len else 0) + len(r)
        return out

    # ---------- моушн-плашки ----------
    def overlay(self, o: TextOverlay):
        st = o.style
        start, end = o.start, (self.total if o.end < 0 else o.end)
        align, mv, yc = self.pos(o.position)
        xc = self.W // 2
        accent = inline_color(st.accent)
        txt = (o.text.upper() if st.uppercase else o.text)

        if o.kind == "title":
            name = self.style(st, st.size or 104, 5, 0)
            anim = f"{{\\pos({xc},{yc})\\fad(120,180)\\fscx55\\fscy55\\t(0,260,\\fscx100\\fscy100)}}"
            self.event(start, end, name, anim + wrap(esc(txt), 16), layer=2)

        elif o.kind == "plate":
            name = self.style(st, st.size or 58, 5, 0, box=True)
            x_from = -self.W // 2
            anim = f"{{\\move({x_from},{yc},{xc},{yc},0,320)\\fad(0,180)}}"
            self.event(start, end, name, anim + wrap(esc(txt), 26), layer=2)

        elif o.kind == "cta":
            st_box = st.model_copy(update={"box": True, "box_color": st.accent, "box_opacity": 1.0, "color": "#111111"})
            name = self.style(st_box, st.size or 64, 5, 0, box=True)
            dur_ms = int((end - start) * 1000)
            pulse = "".join(
                f"\\t({t},{t + 300},\\fscx106\\fscy106)\\t({t + 300},{t + 600},\\fscx100\\fscy100)"
                for t in range(500, max(600, dur_ms - 600), 1200)
            )
            anim = f"{{\\pos({xc},{yc})\\fad(150,150)\\fscx70\\fscy70\\t(0,220,\\fscx100\\fscy100){pulse}}}"
            self.event(start, end, name, anim + wrap(esc(txt), 22), layer=3)

        elif o.kind == "stat":
            big = self.style(st.model_copy(update={"color": st.accent, "font": "Montserrat Black"}), st.size or 230, 5, 0)
            small = self.style(st, int((st.size or 230) * 0.27) or 62, 5, 0)
            value = o.value or ""
            m = re.search(r"\d[\d\s.,]*", value)
            y_num, y_cap = yc - int(70 * self.k), yc + int(110 * self.k)
            if m:
                prefix, num_raw, suffix = value[: m.start()], m.group(0), value[m.end():]
                num_clean = num_raw.replace(" ", "").replace(" ", "").replace(",", ".")
                try:
                    target = float(num_clean)
                except ValueError:
                    target = None
                decimals = len(num_clean.split(".")[1]) if "." in num_clean and target is not None else 0
                if target is not None:
                    steps, count_t = 16, min(0.8, (end - start) * 0.4)
                    for i in range(steps):
                        t0 = start + count_t * i / steps
                        t1 = start + count_t * (i + 1) / steps
                        v = target * (1 - (1 - (i + 1) / steps) ** 3)  # ease-out
                        shown = f"{v:,.{decimals}f}".replace(",", " ") if target >= 1000 else f"{v:.{decimals}f}"
                        self.event(t0, t1, big, f"{{\\pos({xc},{y_num})}}" + esc(prefix + shown + suffix), layer=2)
                    shown = num_raw.strip()
                    self.event(start + count_t, end, big,
                               f"{{\\pos({xc},{y_num})\\t(0,120,\\fscx112\\fscy112)\\t(120,260,\\fscx100\\fscy100)}}"
                               + esc(prefix + shown + suffix), layer=2)
                else:
                    self.event(start, end, big, f"{{\\pos({xc},{y_num})\\fad(120,150)}}" + esc(value), layer=2)
            else:
                self.event(start, end, big, f"{{\\pos({xc},{y_num})\\fad(120,150)}}" + esc(value), layer=2)
            if txt:
                self.event(start + 0.25, end, small,
                           f"{{\\pos({xc},{y_cap})\\fad(200,150)}}" + wrap(esc(txt), 24), layer=2)

        elif o.kind == "list":
            name = self.style(st.model_copy(update={"box": True}), st.size or 56, 4, 0, box=True)
            n = max(1, len(o.items))
            gap = int(120 * self.k)
            y0 = yc - (n - 1) * gap // 2
            x = int(110 * self.k)
            for i, item in enumerate(o.items):
                t0 = start + i * o.stagger
                y = y0 + i * gap
                bullet = f"{{\\c{accent}}}●{{\\c{inline_color(st.color)}}} "
                anim = f"{{\\move({x - int(80 * self.k)},{y},{x},{y},0,250)\\fad(200,150)}}"
                it = item.upper() if st.uppercase else item
                self.event(t0, end, name, anim + bullet + esc(it), layer=2)

    # ---------- сборка ----------
    def render(self) -> str:
        head = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {self.W}",
            f"PlayResY: {self.H}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding",
            *self.styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *self.events,
            "",
        ]
        return "\n".join(head)


def build_ass(req: RenderRequest, total: float) -> str:
    b = AssBuilder(req, total)
    for o in req.texts:
        b.overlay(o)
    if req.captions and req.captions.segments:
        b.captions(req.captions)
    return b.render() if b.events else ""
