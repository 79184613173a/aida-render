"""AiДа Render — HTTP-API рендера роликов для n8n.

POST /render         таймлайн JSON -> job_id (в очередь), по готовности POST на callback_url
GET  /jobs/{id}      статус задания
GET  /files/{name}   готовый mp4 / jpg (без ключа: имя — случайный uuid)
POST /scenes         границы планов рефа и чистые окна без склеек
POST /probe          длительность и параметры файла по URL (сколько длится озвучка и т.п.)
GET  /health         жив ли сервис, версия ffmpeg, длина очереди
"""
import asyncio
import json
import logging
import os
import shutil
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .board import BoardRequest, build_board
from .frames import FramesRequest, build_frames, to_b64
from .models import RenderRequest
from .render import Downloader, RenderError, ffmpeg_version, probe, render
from .scenes import ScenesRequest, build_windows, detect_cuts

API_KEY = os.getenv("RENDER_API_KEY", "")
DATA = Path(os.getenv("DATA_DIR", "/data"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
WORKERS = max(1, int(os.getenv("WORKERS", "1")))
TTL_H = float(os.getenv("FILE_TTL_HOURS", "72"))
KEEP_WORK = os.getenv("KEEP_WORK", "0") == "1"

OUT, WORK, JOBS = DATA / "out", DATA / "work", DATA / "jobs"
for d in (OUT, WORK, JOBS):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aida-render")
app = FastAPI(title="AiДа Render", version="1.0.0")
queue: "asyncio.Queue[str]" = asyncio.Queue()


def auth(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "bad api key")


def job_path(jid: str) -> Path:
    return JOBS / f"{jid}.json"


def load_job(jid: str) -> dict:
    p = job_path(jid)
    if not p.exists():
        raise HTTPException(404, "job not found")
    return json.loads(p.read_text())


def save_job(job: dict):
    job["updated_at"] = time.time()
    tmp = job_path(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False))
    tmp.replace(job_path(job["id"]))


def public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "request"}


async def send_callback(job: dict):
    url = job.get("request", {}).get("callback_url")
    if not url:
        return
    payload = public(job)
    async with httpx.AsyncClient(timeout=30) as c:
        for attempt in range(5):
            try:
                r = await c.post(url, json=payload)
                if r.status_code < 500:
                    return
            except httpx.HTTPError as e:
                log.warning("callback %s: %s", job["id"], e)
            await asyncio.sleep(3 * (attempt + 1))


async def worker(n: int):
    while True:
        jid = await queue.get()
        job = load_job(jid)
        job.update(status="rendering", started_at=time.time())
        save_job(job)
        log.info("worker %s: render %s", n, jid)
        work = WORK / jid
        out_file = OUT / f"{jid}.mp4"
        try:
            req = RenderRequest(**job["request"])

            def step(msg):
                job["step"] = msg
                save_job(job)

            res = await asyncio.to_thread(render, req, work, out_file, step)
            job.update(status="done", url=f"{BASE_URL}/files/{jid}.mp4", thumb_url=f"{BASE_URL}/files/{jid}.jpg",
                       duration=res["duration"], size_mb=res["size_mb"], scene_starts=res["scene_starts"],
                       render_sec=round(time.time() - job["started_at"], 1), step="готово")
        except RenderError as e:
            job.update(status="error", error=str(e)[:2000])
        except Exception as e:  # noqa
            log.error(traceback.format_exc())
            job.update(status="error", error=f"{type(e).__name__}: {e}"[:2000])
        finally:
            save_job(job)
            if not KEEP_WORK:
                shutil.rmtree(work, ignore_errors=True)
            queue.task_done()
        log.info("job %s -> %s", jid, job["status"])
        await send_callback(job)


async def janitor():
    while True:
        cutoff = time.time() - TTL_H * 3600
        for folder in (OUT, JOBS):
            for f in folder.iterdir():
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except FileNotFoundError:
                    pass
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup():
    # задания, которые висели при перезапуске, — заново в очередь
    for p in sorted(JOBS.glob("*.json"), key=lambda x: x.stat().st_mtime):
        job = json.loads(p.read_text())
        if job.get("status") in ("queued", "rendering"):
            job["status"] = "queued"
            save_job(job)
            queue.put_nowait(job["id"])
    for n in range(WORKERS):
        asyncio.create_task(worker(n))
    asyncio.create_task(janitor())
    log.info("AiДа Render up: %s, workers=%s", ffmpeg_version(), WORKERS)


@app.get("/health")
async def health():
    return {"ok": True, "version": "2026-11-18.1", "ffmpeg": ffmpeg_version(), "queue": queue.qsize(), "workers": WORKERS}


@app.post("/render", dependencies=[Depends(auth)])
async def create_render(req: RenderRequest):
    jid = uuid.uuid4().hex
    job = {"id": jid, "status": "queued", "created_at": time.time(), "meta": req.meta,
           "position": queue.qsize() + 1, "request": req.model_dump()}
    save_job(job)
    await queue.put(jid)
    return {"job_id": jid, "status": "queued", "position": job["position"],
            "status_url": f"{BASE_URL}/jobs/{jid}",
            "total_duration": round(sum(s.duration for s in req.scenes), 3)}


@app.get("/jobs/{jid}", dependencies=[Depends(auth)])
async def get_job(jid: str):
    return public(load_job(jid))


@app.get("/files/{name}")
async def get_file(name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    p = OUT / name
    if not p.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(p)


@app.post("/storyboard", dependencies=[Depends(auth)])
async def storyboard(req: BoardRequest):
    """Сетка кадров одной картинкой. Синхронно: обычно 2–5 секунд."""
    if not req.frames:
        raise HTTPException(400, "нет кадров")
    data = await asyncio.to_thread(build_board, req)
    name = f"board_{uuid.uuid4().hex}.jpg"
    (OUT / name).write_bytes(data)
    return {"url": f"{BASE_URL}/files/{name}", "frames": len(req.frames)}


@app.post("/frames", dependencies=[Depends(auth)])
async def frames(req: FramesRequest):
    """Кадры видео сеткой с разметкой 10% — для агента, который ищет на рефе титры и логотипы."""
    work = WORK / f"frames_{uuid.uuid4().hex}"
    dl = Downloader(work)
    try:
        path = await asyncio.to_thread(dl.get, req.url)
        info = await asyncio.to_thread(probe, path)
        res = await asyncio.to_thread(build_frames, req, path, float(info.get("duration") or 0) or 1.0)
        name = f"frames_{uuid.uuid4().hex}.jpg"
        (OUT / name).write_bytes(res["data"])
        return {"url": f"{BASE_URL}/files/{name}", "b64": to_b64(res["data"]), "media_type": "image/jpeg",
                "times": res["times"], "columns": res["columns"], "cell": res["cell"],
                "duration": round(float(info.get("duration") or 0), 2)}
    except RenderError as e:
        raise HTTPException(400, str(e))
    finally:
        dl.close()
        shutil.rmtree(work, ignore_errors=True)


@app.post("/scenes", dependencies=[Depends(auth)])
async def scenes(req: ScenesRequest):
    """Склейки в видео и чистые окна внутри одного плана: из них монтажёр берёт куски рефа."""
    work = WORK / f"scenes_{uuid.uuid4().hex}"
    dl = Downloader(work)
    try:
        path = await asyncio.to_thread(dl.get, req.url)
        info = await asyncio.to_thread(probe, path)
        dur = float(info.get("duration") or 0)
        cuts = await asyncio.to_thread(detect_cuts, path, req.threshold)
        wins = build_windows(cuts, dur, req.min_len, req.margin)
        return {"duration": round(dur, 2), "cuts": cuts, "windows": wins}
    except RenderError as e:
        raise HTTPException(400, str(e))
    finally:
        dl.close()
        shutil.rmtree(work, ignore_errors=True)


class ProbeRequest(BaseModel):
    url: str


@app.post("/probe", dependencies=[Depends(auth)])
async def probe_url(body: ProbeRequest):
    work = WORK / f"probe_{uuid.uuid4().hex}"
    dl = Downloader(work)
    try:
        path = await asyncio.to_thread(dl.get, body.url)
        return await asyncio.to_thread(probe, path)
    except RenderError as e:
        raise HTTPException(400, str(e))
    finally:
        dl.close()
        shutil.rmtree(work, ignore_errors=True)
