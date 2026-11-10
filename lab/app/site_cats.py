"""catcam.machaka.net: Simba and Nala. Pages:

  /                 today: the flash of the day, the camera now, today's numbers and frames
  /flashes          every flash so far; /flashes/<day> one of them with its moments
  /camera           the camera now, any moment of the last 14 days, an hour-by-hour day, Frigate's detections
  /weight           each cat's weight, trend and weigh-ins
  /status           the jobs behind the site (flash, feeding monitor), Frigate, the monitor's runs

Media (Frigate is home-network only, so the lab fetches and serves it): /flash/<day>.mp4|jpg, /media/latest.jpg,
/media/frame/<ts>.jpg, /media/clip/<start>-<end>.mp4, /media/event/<id>/thumbnail.jpg and clip.mp4.
"""
import asyncio
import datetime as dt
import json
import math
import os
import re
import time
import uuid

import httpx
from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import charts, store
from .config import TZ
from .sources import cats, k8s
from .web import page, site

app = site("catcam", [("/", "Today", "home"), ("/flashes", "Flashes", "film"), ("/camera", "Camera", "camera"),
                      ("/weight", "Weight", "scale"), ("/status", "Status", "gauge")])
FLASH = cats.FLASH
DAY = cats.DAY
JOBS = {"cat-flash": ("Flash of the day", "Looks through the day's recordings with the cats' own model and stitches "
                                          "the moments with Simba or Nala into a short video."),
        "cat-monitor": ("Feeding monitor", "Watches the bowl overnight and counts which cat is in view. Without the "
                                           "tag on the bowl it can't measure feeding time.")}


def flashes(limit=14):
    """Kept for the Homepage tile (summary.catcam): built flashes, newest first."""
    return cats.flashes(limit)


def _weights():
    w = cats.weights.get()
    return w, (cats.weight_cards(w.data) if w.ok and w.data else [])


def _cron(name):
    j = k8s.jobs.get()
    return ((j.data or {}).get("cronjobs") or {}).get(name)


def schedule_words(schedule, now=None):
    """'20 0,19 * * *' -> ('Every day at 00:20 and 19:20', next run as epoch). Other schedules: (schedule, None)."""
    m = re.fullmatch(r"(\d+) ([\d,]+) \* \* \*", (schedule or "").strip())
    if not m:
        return schedule or "-", None
    minute, hours = int(m.group(1)), sorted(int(h) for h in m.group(2).split(","))
    times = [f"{h:02d}:{minute:02d}" for h in hours]
    words = "Every day at " + (" and ".join(times) if len(times) <= 2 else ", ".join(times[:-1]) + " and " + times[-1])
    now_dt = dt.datetime.fromtimestamp(now or time.time(), TZ)
    for add in (0, 1):
        d = now_dt.date() + dt.timedelta(days=add)
        for h in hours:
            t = dt.datetime(d.year, d.month, d.day, h, minute, tzinfo=TZ)
            if t > now_dt:
                return words, t.timestamp()
    return words, None


def job_card(name):
    """A CronJob for the status page: what it does, when, paused or not, its last runs (Kubernetes keeps a few;
    the lab remembers them for 30 days)."""
    c = _cron(name)
    title, what = JOBS[name]
    words, nxt = schedule_words(c["schedule"] if c else "")
    live = [j for j in ((k8s.jobs.get().data or {}).get("jobs") or [])
            if j.get("cronjob") == name or j["name"].startswith(name + "-")]
    runs = {r["name"]: r for r in store.job_history(name, days=30)}
    for j in live:       # what Kubernetes still has is the freshest
        runs[j["name"]] = {**runs.get(j["name"], {}), **j}
    runs = sorted(runs.values(), key=lambda r: r.get("start") or 0, reverse=True)
    return {"name": name, "title": title, "what": what, "found": bool(c), "paused": bool(c and c["suspended"]),
            "when": words, "next": None if (c and c["suspended"]) else nxt, "last": runs[0] if runs else None,
            "runs": runs, "last_success": c.get("last_success") if c else None}


# ---- pages -----------------------------------------------------------------------------------------------------

@app.exception_handler(StarletteHTTPException)
async def missing(request: Request, exc: StarletteHTTPException):
    """A page that isn't there gets the site's own page; pictures and videos keep their plain answer."""
    root = request.scope.get("root_path", "").rstrip("/")
    rel = request.url.path[len(root):] if root and request.url.path.startswith(root) else request.url.path
    if exc.status_code != 404 or rel.startswith(("/media/", "/flash/", "/static/")):
        return await http_exception_handler(request, exc)
    resp = page(request, "catcam/missing.html", detail=exc.detail if exc.detail != "Not Found" else "")
    resp.status_code = 404
    return resp


@app.get("/", response_class=HTMLResponse)
def today(request: Request, day: str = "", at: str = ""):
    root = request.scope.get("root_path", "").rstrip("/")
    if DAY.fullmatch(day):              # links from the old one-page site
        return RedirectResponse(f"{root}/flashes/{day}", 302)
    if at:
        return RedirectResponse(f"{root}/camera?at={at}", 302)
    now_ts = int(time.time())
    all_flash = cats.flashes(8)
    hour = now_ts - now_ts % 3600
    frames = [hour - 3600 * i for i in range(12) if now_ts - (hour - 3600 * i) > 60]
    w, cards = _weights()
    return page(request, "catcam/today.html", flash=all_flash[0] if all_flash else None, others=all_flash[1:],
                now_ts=now_ts, frames=frames, weights=w, cards=cards,
                today_words=cats.day_words(dt.datetime.now(TZ).date().isoformat()),
                next_build=schedule_words((_cron("cat-flash") or {}).get("schedule", ""))[1])


@app.get("/flashes", response_class=HTMLResponse)
def flash_list(request: Request):
    every = cats.flashes(120)
    return page(request, "catcam/flashes.html", flashes=every,
                next_build=schedule_words((_cron("cat-flash") or {}).get("schedule", ""))[1],
                total_moments=sum(f["moments"] for f in every))


@app.get("/flashes/{day}", response_class=HTMLResponse)
def flash_day(request: Request, day: str):
    f = cats.flash(day)
    if not f:
        raise HTTPException(404, "No flash for that day")
    days = cats.flash_days()
    i = days.index(day)
    newer = days[i - 1] if i > 0 else None
    older = days[i + 1] if i + 1 < len(days) else None
    return page(request, "catcam/flash.html", active="/flashes", flash=f,
                newer=cats.flash(newer) if newer else None, older=cats.flash(older) if older else None,
                now_ts=int(time.time()), keep=cats.KEEP_DAYS * 86400)


@app.get("/camera", response_class=HTMLResponse)
def camera(request: Request, day: str = "", at: str = ""):
    now_ts = int(time.time())
    look = None
    if at:
        try:
            t = int(dt.datetime.fromisoformat(at).replace(tzinfo=TZ).timestamp())
            if 0 < now_ts - t < cats.KEEP_DAYS * 86400:
                look = t
        except ValueError:
            pass
    around = [f for f in ((look + o) for o in (-120, -60, 0, 60, 120, 180, 240, 300)) if f < now_ts - 30] if look else []
    days = cats.recent_days(7, now_ts)
    if day not in {d for d, _ in days}:
        day = days[0][0]
    rec = cats.recordings.get()
    hours = (rec.data or {}).get(day, {}) if rec.ok else {}
    frames = []
    for t in cats.hour_frames(day, now_ts):
        h = dt.datetime.fromtimestamp(t, TZ).hour
        frames.append({"ts": t, "hour": h, "events": hours.get(h, {}).get("events", 0),
                       "recorded": not hours or h in hours})
    today_d = dt.datetime.fromtimestamp(now_ts, TZ).date()
    at_of = lambda d, h: dt.datetime(d.year, d.month, d.day, h, tzinfo=TZ)
    picks = [(label, t.strftime("%Y-%m-%dT%H:%M")) for label, t in [
        ("An hour ago", dt.datetime.fromtimestamp(now_ts - 3600, TZ)),
        ("This morning, 8:00", at_of(today_d, 8)),
        ("Last night, 3:00", at_of(today_d, 3)),
        ("Yesterday, 20:00", at_of(today_d - dt.timedelta(days=1), 20))] if t.timestamp() < now_ts - 60]
    return page(request, "catcam/camera.html", now_ts=now_ts, at=at, look=look, around=around, day=day, days=days,
                picks=picks,
                frames=list(reversed(frames)), rec=rec, events=cats.events.get(),
                at_value=dt.datetime.fromtimestamp(look or now_ts - 3600, TZ).strftime("%Y-%m-%dT%H:%M"),
                at_min=dt.datetime.fromtimestamp(now_ts - (cats.KEEP_DAYS - 1) * 86400, TZ).strftime("%Y-%m-%dT00:00"),
                at_max=dt.datetime.fromtimestamp(now_ts, TZ).strftime("%Y-%m-%dT%H:%M"))


@app.get("/weight", response_class=HTMLResponse)
def weight(request: Request):
    w, cards = _weights()
    if cards:
        xmin = min(c["points"][0][0] for c in cards)
        xmax = max(c["points"][-1][0] for c in cards)
        for c in cards:
            c["chart"] = weight_chart(c, xmin, xmax)
    return page(request, "catcam/weight.html", weights=w, cards=cards,
                table=cats.weight_table(w.data) if w.ok and w.data else [], cat_names=[c["cat"] for c in cards])


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    last = cats.flashes(1)
    jobs = [job_card("cat-flash"), job_card("cat-monitor")]
    runs = sorted((r for j in jobs for r in j["runs"]), key=lambda r: r.get("start") or 0, reverse=True)
    return page(request, "catcam/status.html", jobs=jobs, runs=runs, last_flash=last[0] if last else None,
                frigate=cats.frigate.get(), events=cats.events.get(), monitor=cats.monitor.get(),
                now_ts=int(time.time()), keep=cats.KEEP_DAYS * 86400)


# ---- the weight chart: one cat, its own scale ---------------------------------------------------------------------

def range_ticks(lo, hi, count=4):
    """Round ticks around lo..hi, not from zero: a cat's weight moves within a few hundred grams."""
    if hi - lo < 0.2:
        mid = (hi + lo) / 2
        lo, hi = mid - 0.1, mid + 0.1
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step + 1e-9) * step
    n = int(math.ceil((hi - start) / step - 1e-9))
    return [round(start + i * step, 6) for i in range(n + 1)]


def weight_chart(card, xmin, xmax, height=150):
    """One cat's weigh-ins on its own kg scale over the same span as the other cat's (small multiples), with the
    classes and data of charts.lines, so lab.js gives it the same crosshair and tooltip."""
    pts = card["points"]
    if xmax <= xmin:
        xmax = xmin + 1
    ticks = range_ticks(card["low"], card["high"])
    x0, x1, y0, y1 = charts.PAD_L, charts.W - charts.PAD_R, charts.PAD_T, height - charts.PAD_B
    x_of = lambda x: x0 + (x1 - x0) * (x - xmin) / (xmax - xmin)
    y_of = lambda v: y1 - (y1 - y0) * (v - ticks[0]) / ((ticks[-1] - ticks[0]) or 1)
    parts = []
    for t in ticks:
        y = y_of(t)
        parts.append(f'<line class="grid" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{x0 - 5}" y="{y + 3.5:.1f}" text-anchor="end">{t:g}</text>')
    first = dt.datetime.fromtimestamp(xmin, TZ)
    months, m = [], first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while m.timestamp() <= xmax:
        if m.timestamp() >= xmin:
            months.append(m)
        m = (m + dt.timedelta(days=32)).replace(day=1)
    for mo in months[::max(1, -(-len(months) // 4))]:
        parts.append(f'<text class="tick" x="{x_of(mo.timestamp()):.1f}" y="{height - 6}" text-anchor="middle">'
                     f'{mo:%b} ’{mo:%y}</text>')
    xy = [(x_of(x), y_of(v)) for x, v in pts]
    path = "M" + "L".join(f"{px:.1f},{py:.1f}" for px, py in xy)
    slot = card["slot"]
    parts.append(f'<path class="wash s{slot}" d="{path}V{y1}H{xy[0][0]:.1f}Z"/>')
    parts.append(f'<path class="line s{slot}" d="{path}"/>')
    ex, ey = xy[-1]
    parts.append(f'<circle class="dot s{slot}" cx="{ex:.1f}" cy="{ey:.1f}" r="4"/>')
    data = {"x0": x0, "x1": x1, "xmin": xmin, "xmax": xmax, "unit": " kg",
            "series": [{"name": card["cat"], "slot": slot, "points": pts}]}
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{y0}" y2="{y1}" visibility="hidden"/>')
    parts.append(f'<rect class="hit-area" x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"/>')
    return charts._svg(parts, height, f"{card['cat']}'s weight in kg", data=json.dumps(data))


# ---- the flash files ---------------------------------------------------------------------------------------------

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


@app.get("/media/event/{eid}/snapshot.jpg")
async def event_snapshot(eid: str):
    """The frame Frigate kept of an event (640x360 JPEG); its thumbnail is a 3 KB crop, in WebP."""
    if not EVENT_ID.fullmatch(eid):
        raise HTTPException(404)
    return await _proxy(cats.event_snapshot_url(eid), cache=86400)


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
    return await _proxy(cats.latest_url(), {"height": 540}, cache=5)


@app.get("/media/clip/{start}-{end}.mp4")
async def clip(start: int, end: int):
    if not (0 < end - start <= 600 and 0 < time.time() - start < 15 * 86400):
        raise HTTPException(400, "Clips are up to 10 minutes from the last 14 days")
    return await _clip(cats.range_clip_url(start, end), f"range-{start}-{end}.mp4")
