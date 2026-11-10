"""catcam.machaka.net: the day's flash (a short video of the cats' moments), the camera now and at any time in the
last 14 days, their weights, and what the feeding monitor saw."""
import asyncio
import datetime as dt
import json
import os
import re
import time
import uuid

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from . import charts, store
from .config import CATS_DIR, TZ
from .sources import cats, k8s
from .web import page, site

app = site("catcam", [("/", "Today", "home"), ("/flashes", "Flashes", "film"), ("/camera", "Camera", "camera"),
                     ("/weight", "Weight", "scale"), ("/status", "Status", "gauge")])
FLASH = os.path.join(CATS_DIR, "flash")
DAY = re.compile(r"\d{4}-\d\d-\d\d")


def flashes(limit=14):
    """Built flash videos, newest first: [{day, seconds, moments, cats, built}] from each video's JSON."""
    out = []
    try:
        names = sorted((n for n in os.listdir(FLASH) if n.endswith(".json")), reverse=True)
    except FileNotFoundError:
        return out
    for n in names[:limit]:
        try:
            meta = json.load(open(os.path.join(FLASH, n), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        day = n[:-5]
        if not os.path.exists(os.path.join(FLASH, day + ".mp4")):
            continue
        d = dt.date.fromisoformat(day)
        first, each = meta.get("title_seconds", 1.8), meta.get("clip_seconds", 4.0)
        items = [{**it, "seek": first + i * each,
                  "at_label": dt.datetime.fromtimestamp(it.get("at") or it["start"], TZ).strftime("%H:%M")}
                 for i, it in enumerate(it for it in meta.get("items", []) if it.get("start"))]
        out.append({**meta, "day": day, "day_label": f"{d:%a} {d.day} {d:%b}", "items": items,
                    "moments": meta.get("moments", len(items)), "seconds": meta.get("seconds", 0)})
    return out


@app.get("/", response_class=HTMLResponse)
def home(request: Request, at: str = "", day: str = ""):
    now_ts = int(time.time())
    # "Look back": frames around a chosen moment (default: the last hours, one frame each).
    look = None
    if at:
        try:
            t = int(dt.datetime.fromisoformat(at).replace(tzinfo=TZ).timestamp())
            if 0 < now_ts - t < 14 * 86400:
                look = t
        except ValueError:
            pass
    if look:
        frames = [look + o for o in (-120, -60, 0, 60, 120, 180, 240, 300)]
    else:
        hour = now_ts - now_ts % 3600
        frames = [hour - 3600 * i for i in range(7, -1, -1)]
    frames = [f for f in frames if f < now_ts - 30]
    w = cats.weights.get()
    weight_chart = None
    if w.ok and w.data:
        series = [{"name": c, "slot": i + 1, "points": pts} for i, (c, pts) in enumerate(sorted(w.data.items()))]
        xs = [p[0] for s in series for p in s["points"]]
        lo = dt.datetime.fromtimestamp(min(xs), TZ)
        months, m = [], lo.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while m.timestamp() <= max(xs):
            months.append(m)
            m = (m + dt.timedelta(days=32)).replace(day=1)
        step = max(1, -(-len(months) // 6))
        weight_chart = charts.lines(series, title="Weight, kg", unit=" kg",
                                    x_labels=[(m.timestamp(), f"{m:%b %y}") for m in months[::step]])
    all_flash = flashes()
    shown = next((f for f in all_flash if f["day"] == day), all_flash[0] if all_flash else None)
    ev = cats.events.get()
    return page(request, "cats.html", flash=shown, flashes=all_flash, today=dt.datetime.now(TZ).date().isoformat(),
                frames=frames, look=look, at=at, now_ts=now_ts,
                weights=w, weight_chart=weight_chart, monitor=cats.monitor.get(), events=ev,
                runs=store.job_history("cat-monitor", days=30) + store.job_history("cat-flash", days=30),
                cron=((k8s.jobs.get().data or {}).get("cronjobs") or {}))


@app.get("/flash/{name}")
def flash_file(name: str):
    """A flash video (.mp4, served with Range for iPhones) or its poster (.jpg)."""
    day, _, ext = name.partition(".")
    if not DAY.fullmatch(day) or ext not in ("mp4", "jpg"):
        raise HTTPException(404)
    path = os.path.join(FLASH, f"{day}.{ext}")
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4" if ext == "mp4" else "image/jpeg",
                        headers={"Cache-Control": "private, max-age=600"})


# ---- Frigate media, proxied (Frigate is home-network only) ---------------------------------------------------

_frigate = httpx.AsyncClient(timeout=httpx.Timeout(10, read=120))
EVENT_ID = re.compile(r"\d+\.\d+-[a-z0-9]+")


async def _proxy(url, params=None, cache=300):
    """Only images come through here, always labelled as such (never Frigate's own pages)."""
    try:
        r = await _frigate.send(_frigate.build_request("GET", url, params=params), stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Frigate: {type(e).__name__}")
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        await r.aclose()
        raise HTTPException(404 if r.status_code < 500 else 502, "Frigate has no such image")
    headers = {"Cache-Control": f"private, max-age={cache}", "X-Content-Type-Options": "nosniff"}
    if "content-length" in r.headers:
        headers["Content-Length"] = r.headers["content-length"]

    async def close():
        await r.aclose()
    return StreamingResponse(r.aiter_raw(), media_type="image/jpeg", headers=headers, background=close)


CLIPS = os.path.join(os.path.dirname(store.LAB_DB) or ".", "clips")
CLIPS_MAX_BYTES = 400 * 2**20


def _trim_clips():
    """Drop clips older than 2 hours, then the oldest while over the size cap (files may vanish meanwhile)."""
    files = []
    for f in os.listdir(CLIPS):
        try:
            st = os.stat(os.path.join(CLIPS, f))
            files.append((st.st_mtime, st.st_size, os.path.join(CLIPS, f)))
        except OSError:
            continue
    total = sum(f[1] for f in files)
    for mtime, size, f in sorted(files):
        if time.time() - mtime > 7200 or total > CLIPS_MAX_BYTES:
            try:
                os.remove(f)
                total -= size
            except OSError:
                pass


async def _clip(url, name):
    """A clip saved to disk first (streamed, never held in memory), then served with Range support: iPhones only
    play video they can seek in."""
    os.makedirs(CLIPS, exist_ok=True)
    path = os.path.join(CLIPS, name)
    if not os.path.exists(path):
        await asyncio.to_thread(_trim_clips)
        tmp = f"{path}.{uuid.uuid4().hex}.part"
        try:
            async with _frigate.stream("GET", url) as r:
                if r.status_code != 200:
                    raise HTTPException(404 if r.status_code < 500 else 502, "Frigate has no such clip")
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        f.write(chunk)
            os.replace(tmp, path)
        except httpx.HTTPError as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise HTTPException(502, f"Frigate: {type(e).__name__}")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/media/event/{eid}/thumbnail.jpg")
async def event_thumbnail(eid: str):
    if not EVENT_ID.fullmatch(eid):
        raise HTTPException(404)
    return await _proxy(f"{cats.FRIGATE}/api/events/{eid}/thumbnail.jpg", cache=86400)


@app.get("/media/event/{eid}/clip.mp4")
async def event_clip(eid: str):
    if not EVENT_ID.fullmatch(eid):
        raise HTTPException(404)
    return await _clip(cats.event_clip_url(eid), f"event-{eid}.mp4")


@app.get("/media/frame/{ts}.jpg")
async def frame(ts: int):
    if not 0 < time.time() - ts < 15 * 86400:
        raise HTTPException(404)
    return await _proxy(cats.frame_url(ts), {"height": 360}, cache=86400)


@app.get("/media/latest.jpg")
async def latest():
    return await _proxy(cats.latest_url(), {"height": 480}, cache=5)


@app.get("/media/clip/{start}-{end}.mp4")
async def clip(start: int, end: int):
    if not (0 < end - start <= 600 and 0 < time.time() - start < 15 * 86400):
        raise HTTPException(400, "Clips are up to 10 minutes from the last 14 days")
    return await _clip(cats.range_clip_url(start, end), f"range-{start}-{end}.mp4")
