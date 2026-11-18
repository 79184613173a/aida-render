"""Склейка озвучки из блоков: нормализация громкости каждого куска, паузы между ними, один mp3 и тайминги блоков."""
import subprocess
from pathlib import Path
from typing import List

from pydantic import BaseModel


class AudioConcatRequest(BaseModel):
    urls: List[str]
    gap: float = 0.15          # пауза между блоками, сек
    lufs: float = -16.0        # целевая громкость каждого куска


def _dur(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def concat_audio(paths: List[str], out: str, gap: float, lufs: float) -> List[dict]:
    """Каждый кусок -> wav 44.1k mono с loudnorm + тишина gap в конце (кроме последнего), потом склейка в mp3."""
    work = Path(out).parent
    norm = []
    for i, p in enumerate(paths):
        n = str(work / f"n{i}.wav")
        af = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
        if i < len(paths) - 1 and gap > 0:
            af += f",apad=pad_dur={gap}"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", p, "-af", af, "-ar", "44100", "-ac", "1", n],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg блок {i + 1}: {r.stderr[-300:]}")
        norm.append(n)
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{n}'\n" for n in norm))
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c:a", "libmp3lame", "-b:a", "192k", out], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"склейка: {r.stderr[-300:]}")
    parts, t = [], 0.0
    for i, n in enumerate(norm):
        d = _dur(n)
        end = t + d - (gap if i < len(norm) - 1 else 0)
        parts.append({"n": i + 1, "start": round(t, 2), "end": round(end, 2), "dur": round(end - t, 2)})
        t += d
    return parts
