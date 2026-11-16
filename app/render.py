"""Движок: таймлайн JSON -> mp4.

Проход 1: каждая сцена нормализуется в отдельный клип WxH@fps (crop / blur-фон / Ken Burns для картинок).
Проход 2: склейка клипов с переходами xfade. Переход «съедает» хвост уходящей сцены, поэтому
          время начала каждой сцены = сумма длительностей предыдущих (агентам удобно считать).
Проход 3: картинки-оверлеи + субтитры/плашки (ASS) + микс звука (голос, музыка с даккингом, SFX,
          родной звук клипов) + нормализация громкости. H.264 + AAC, faststart.
"""
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

import httpx

from .ass import build_ass
from .models import RenderRequest, Scene

FONTS_DIR = os.getenv("FONTS_DIR", str(Path(__file__).resolve().parent.parent / "fonts"))
MAX_DOWNLOAD = int(os.getenv("MAX_DOWNLOAD_MB", "500")) * 1024 * 1024
FFMPEG_THREADS = os.getenv("FFMPEG_THREADS", "0")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic"}


class RenderError(Exception):
    pass


def run(cmd: List[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or "")[-1500:]
        raise RenderError(f"ffmpeg упал ({cmd[0]}): {tail}")
    return p


def clean_image(src: str, dst: Path) -> str:
    """Пересохраняет картинку в чистый RGBA PNG. Не читается — понятная ошибка вместо зависшего ffmpeg."""
    from PIL import Image
    try:
        with Image.open(src) as im:
            im.convert("RGBA").save(dst, "PNG")
    except Exception as e:  # noqa
        raise RenderError(f"картинка-оверлей не читается: {e}")
    return str(dst)


def ffmpeg_version() -> str:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    except Exception as e:  # noqa
        return f"ffmpeg не найден: {e}"


def probe(path: str) -> dict:
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height,duration", "-of", "json", path], timeout=120)
    data = json.loads(p.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = float(data.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    return {"duration": dur, "has_video": bool(v), "has_audio": bool(a),
            "width": (v or {}).get("width"), "height": (v or {}).get("height")}


class Downloader:
    def __init__(self, work: Path):
        self.dir = work / "src"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache = {}
        self.client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120, connect=20),
                                   headers={"User-Agent": "AiDa-Render/1.0"})

    def get(self, url: str) -> str:
        if url in self.cache:
            return self.cache[url]
        if url.startswith("file://"):
            path = url[7:]
            if not os.path.exists(path):
                raise RenderError(f"нет файла {path}")
            self.cache[url] = path
            return path
        h = hashlib.sha1(url.encode()).hexdigest()[:16]
        ext = os.path.splitext(url.split("?")[0])[1].lower()[:6]
        tmp = self.dir / f"{h}.part"
        size = 0
        try:
            with self.client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise RenderError(f"не скачался {url[:120]}: HTTP {r.status_code}")
                if not ext:
                    ext = mimetypes.guess_extension((r.headers.get("content-type") or "").split(";")[0]) or ".bin"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            raise RenderError(f"файл больше {MAX_DOWNLOAD >> 20} МБ: {url[:120]}")
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise RenderError(f"не скачался {url[:120]}: {e}")
        path = self.dir / f"{h}{ext}"
        tmp.rename(path)
        self.cache[url] = str(path)
        return str(path)

    def close(self):
        self.client.close()


def is_image(scene: Scene, path: str) -> bool:
    if scene.type != "auto":
        return scene.type == "image"
    if os.path.splitext(path)[1].lower() in IMAGE_EXT:
        return True
    info = probe(path)
    return info["has_video"] and info["duration"] < 0.2


def enc_args(quality: str, fps: int, final: bool = False) -> List[str]:
    if final:
        crf = "21" if quality == "final" else "28"
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-maxrate", "6M", "-bufsize", "12M",
                "-pix_fmt", "yuv420p", "-r", str(fps), "-threads", FFMPEG_THREADS]
    # промежуточные клипы — почти без потерь
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
            "-r", str(fps), "-threads", FFMPEG_THREADS]


def normalize_scene(i: int, s: Scene, src: str, out: Path, W: int, H: int, fps: int,
                    seg_dur: float, quality: str) -> dict:
    """Одна сцена -> клип ровно seg_dur секунд, WxH, без звука (+ отдельный wav, если keep_audio)."""
    seg = out / f"seg_{i:03d}.mp4"
    image = is_image(s, src)
    cover = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    blur = (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:4,"
            f"eq=brightness=-0.08[bg];[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    base = blur if s.fit == "blur" else cover
    audio_path = None

    if image:
        frames = max(1, int(round(seg_dur * fps)))
        if s.kenburns == "none":
            vf = f"{base},fps={fps},format=yuv420p"
            cmd = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(fps), "-i", src, "-t", f"{seg_dur:.3f}",
                   "-filter_complex", vf, "-an", *enc_args(quality, fps), str(seg)]
        else:
            # zoompan гладко работает на увеличенной картинке
            W2, H2 = W * 2, H * 2
            z = 0.14
            if s.kenburns == "in":
                zexpr, xexpr, yexpr = f"1+{z}*on/{frames}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
            elif s.kenburns == "out":
                zexpr, xexpr, yexpr = f"{1 + z}-{z}*on/{frames}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
            elif s.kenburns == "left":
                zexpr, xexpr, yexpr = f"{1 + z}", f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"
            else:
                zexpr, xexpr, yexpr = f"{1 + z}", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
            pre = base.replace(f"{W}:{H}", f"{W2}:{H2}").replace(f"crop={W2}:{H2}", f"crop={W2}:{H2}")
            vf = (f"{pre},zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':d={frames}:s={W}x{H}:fps={fps},"
                  f"setsar=1,format=yuv420p")
            cmd = ["ffmpeg", "-y", "-i", src, "-filter_complex", vf, "-frames:v", str(frames), "-an",
                   *enc_args(quality, fps), str(seg)]
        run(cmd)
    else:
        info = probe(src)
        avail = max(0.0, info["duration"] - s.trim_start)
        pad = max(0.0, seg_dur - avail) + 0.1
        vf = f"{base},fps={fps},tpad=stop_mode=clone:stop_duration={pad:.3f},format=yuv420p"
        cmd = ["ffmpeg", "-y", "-ss", f"{s.trim_start:.3f}", "-i", src, "-t", f"{seg_dur:.3f}",
               "-filter_complex", vf, "-an", "-t", f"{seg_dur:.3f}", *enc_args(quality, fps), str(seg)]
        run(cmd)
        if s.keep_audio and info["has_audio"]:
            audio_path = str(out / f"seg_{i:03d}.wav")
            run(["ffmpeg", "-y", "-ss", f"{s.trim_start:.3f}", "-i", src, "-t", f"{seg_dur:.3f}", "-vn",
                 "-ac", "2", "-ar", "48000", audio_path])
    return {"video": str(seg), "audio": audio_path, "image": image}


def join_scenes(req: RenderRequest, segs: List[dict], out: Path) -> str:
    """Склейка с переходами. Возвращает путь к немому видео."""
    joined = out / "joined.mp4"
    if len(segs) == 1:
        shutil.copy(segs[0]["video"], joined)
        return str(joined)
    cmd = ["ffmpeg", "-y"]
    for s in segs:
        cmd += ["-i", s["video"]]
    # одна база и частота у всех входов и после каждой склейки: иначе xfade в ffmpeg 7 падает (timebase / frame rate 1/0)
    parts = [f"[{i}:v]setpts=PTS-STARTPTS,fps={req.fps},format=yuv420p[n{i}]" for i in range(len(segs))]
    prev, t = "[n0]", 0.0
    for i in range(1, len(segs)):
        sc = req.scenes[i - 1]
        t += sc.duration
        label = f"[v{i}]"
        if sc.transition and sc.transition != "none":
            parts.append(f"{prev}[n{i}]xfade=transition={sc.transition}:duration={sc.transition_duration:.3f}:"
                         f"offset={t:.3f},fps={req.fps}{label}")
        else:
            parts.append(f"{prev}[n{i}]concat=n=2:v=1:a=0,fps={req.fps}{label}")
        prev = label
    cmd += ["-filter_complex", ";".join(parts), "-map", prev, "-an", *enc_args(req.quality, req.fps), str(joined)]
    run(cmd)
    return str(joined)


def render(req: RenderRequest, work: Path, out_file: Path, log=print) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    W, H, fps = req.width, req.height, req.fps
    if not req.scenes:
        raise RenderError("нет сцен")
    dl = Downloader(work)
    try:
        # --- 1. сцены
        log("скачиваю и нормализую сцены")
        segs, starts, t = [], [], 0.0
        for i, s in enumerate(req.scenes):
            starts.append(t)
            has_next = i + 1 < len(req.scenes)
            extra = s.transition_duration if (has_next and s.transition and s.transition != "none") else 0.0
            if has_next and extra and (extra >= s.duration or extra >= req.scenes[i + 1].duration):
                raise RenderError(f"сцена {i + 1}: переход длиннее сцены")
            src = dl.get(s.src)
            segs.append(normalize_scene(i, s, src, work, W, H, fps, s.duration + extra, req.quality))
            t += s.duration
        total = t

        # --- 2. склейка
        log("склеиваю")
        joined = join_scenes(req, segs, work)

        # --- 3. оверлеи, текст, звук
        log("накладываю графику и звук")
        cmd = ["ffmpeg", "-y", "-i", joined]
        idx = 1
        vchain, filters = "[0:v]", []

        for k, im in enumerate(req.images):
            # картинку пересохраняем через PIL (битый PNG иначе вешает ffmpeg) и читаем ОДИН раз:
            # повтор кадра делает фильтр loop в памяти, а не -loop 1 на входе (тот копит буфер парсера)
            path = clean_image(dl.get(im.src), work / f"overlay_{k}.png")
            cmd += ["-i", path]
            end = total if im.end < 0 else min(im.end, total)
            fade = f",fade=t=in:st={im.start:.3f}:d={im.fade}:alpha=1,fade=t=out:st={max(im.start, end - im.fade):.3f}:d={im.fade}:alpha=1" if im.fade > 0 else ""
            filters.append(f"[{idx}:v]scale={int(im.width)}:-1,format=rgba,loop=loop=-1:size=1:start=0,"
                           f"setpts=N/{fps}/TB,trim=duration={end + 0.1:.3f}{fade}[im{k}]")
            filters.append(f"{vchain}[im{k}]overlay=x={im.x}:y={im.y}:eof_action=pass:"
                           f"enable='between(t,{im.start:.3f},{end:.3f})'[vo{k}]")
            vchain = f"[vo{k}]"
            idx += 1

        ass = build_ass(req, total)
        if ass:
            ass_path = work / "subs.ass"
            ass_path.write_text(ass, encoding="utf-8")
            filters.append(f"{vchain}subtitles=filename={ass_path}:fontsdir={FONTS_DIR}[vsub]")
            vchain = "[vsub]"
        filters.append(f"{vchain}format=yuv420p[vout]")

        # звук
        mix, music_label = [], None
        vo_label = None
        if req.voiceover:
            cmd += ["-i", dl.get(req.voiceover.src)]
            d = int(req.voiceover.start * 1000)
            filters.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={req.voiceover.volume},"
                           f"adelay={d}|{d}[vo_raw]")
            idx += 1
            if req.music and req.music.duck:
                filters.append("[vo_raw]asplit=2[vo][vo_sc]")
            else:
                filters.append("[vo_raw]anull[vo]")
            vo_label = "[vo]"
            mix.append(vo_label)
        if req.music:
            m = req.music
            cmd += ["-stream_loop", "-1", "-ss", f"{m.trim_start:.3f}", "-i", dl.get(m.src)]
            d = int(m.start * 1000)
            fo_st = max(0.0, total - m.fade_out)
            filters.append(
                f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{max(0.1, total - m.start):.3f},"
                f"asetpts=PTS-STARTPTS,volume={m.volume},afade=t=in:d={m.fade_in},adelay={d}|{d},"
                f"afade=t=out:st={fo_st:.3f}:d={m.fade_out}[mus_raw]")
            idx += 1
            if vo_label and m.duck:
                filters.append("[mus_raw][vo_sc]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350:makeup=1[mus]")
            else:
                filters.append("[mus_raw]anull[mus]")
            mix.append("[mus]")
        for k, fx in enumerate(req.sfx):
            cmd += ["-i", dl.get(fx.src)]
            d = int(fx.at * 1000)
            filters.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={fx.volume},"
                           f"adelay={d}|{d}[fx{k}]")
            mix.append(f"[fx{k}]")
            idx += 1
        for i, sg in enumerate(segs):
            if sg["audio"]:
                cmd += ["-i", sg["audio"]]
                d = int(starts[i] * 1000)
                vol = req.scenes[i].audio_volume
                filters.append(f"[{idx}:a]aresample=48000,volume={vol},adelay={d}|{d}[sa{i}]")
                mix.append(f"[sa{i}]")
                idx += 1

        if mix:
            pre = "".join(mix)
            amix = f"{pre}amix=inputs={len(mix)}:duration=longest:normalize=0," if len(mix) > 1 else f"{pre}anull,"
            filters.append(f"{amix}apad,atrim=0:{total:.3f},loudnorm=I={req.loudness}:TP=-1.5:LRA=11,"
                           f"aresample=48000[aout]")
            amap = ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        else:
            # тишина, чтобы соцсети не ругались на ролик без аудиодорожки
            cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            amap = ["-map", f"{idx}:a", "-c:a", "aac", "-b:a", "128k"]

        cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", *amap,
                *enc_args(req.quality, fps, final=True), "-t", f"{total:.3f}",
                "-movflags", "+faststart", str(out_file)]
        run(cmd, timeout=3600)

        thumb = out_file.with_suffix(".jpg")
        run(["ffmpeg", "-y", "-ss", f"{min(1.0, total / 3):.2f}", "-i", str(out_file), "-frames:v", "1",
             "-q:v", "3", str(thumb)], timeout=120)
        info = probe(str(out_file))
        return {"duration": round(info["duration"], 2), "size_mb": round(out_file.stat().st_size / 1048576, 2),
                "scene_starts": [round(x, 3) for x in starts], "thumb": str(thumb)}
    finally:
        dl.close()
