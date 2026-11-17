"""Границы планов рефа: где в рефе жёсткие склейки, и какие отрезки «чистые» (внутри одного плана).

Монтажёр берёт куски рефа только из чистых окон, чтобы в ролик не попадали мелькающие кадры соседних планов.
"""
import re
import subprocess
from typing import List

from pydantic import BaseModel


class ScenesRequest(BaseModel):
    url: str
    threshold: float = 0.25   # чувствительность детектора склеек ffmpeg (меньше = ловит больше)
    min_len: float = 0.7      # чистое окно короче этого отбрасываем
    margin: float = 0.1       # отступ от склейки внутрь плана (кадры-вспышки на стыке)


def detect_cuts(src: str, threshold: float) -> List[float]:
    vf = f"select='gt(scene,{threshold})',showinfo"
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", src, "-vf", vf, "-an", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    return sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)})


def build_windows(cuts: List[float], duration: float, min_len: float, margin: float) -> List[dict]:
    bounds = [0.0] + [c for c in cuts if 0.0 < c < duration] + [duration]
    out = []
    for i in range(len(bounds) - 1):
        a = bounds[i] + (margin if i > 0 else 0.0)
        b = bounds[i + 1] - (margin if i < len(bounds) - 2 else 0.05)
        if b - a >= min_len:
            out.append({"a": round(a, 2), "b": round(b, 2), "len": round(b - a, 2)})
    return out
