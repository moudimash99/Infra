"""Lab: the homelab's status site. Pages on :8080 (Ingress lab.machaka.net, behind Cloudflare Access); the PC's
report intake on :8081 (NodePort, home network only, never in the Ingress)."""
import asyncio
import datetime as dt
import logging
import os
import re
import secrets
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import charts, fmt, logs, store
from .config import TOOLS, TZ
from .sources import bgf, bgt, cats, k8s, misc, prom
from .overview import attention, job_cards, machines

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("lab")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
fmt.register(templates.env)
templates.env.globals.update(charts=charts, STAGES=bgt.STAGES)


def page(request, name, **ctx):
    return templates.TemplateResponse(request, name, {"path": request.url.path, **ctx})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/fragment/attention", response_class=HTMLResponse)
def attention_fragment(request: Request):
    return page(request, "_attention.html", attention=attention())


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return page(request, "index.html", attention=attention(), cards=job_cards(), machines=machines(),
                sites=misc.sites.get(), tools=TOOLS, checked=time.time())


# ---- bg-taste-match ---------------------------------------------------------------------------------------------

def day_labels(xs):
    """Bottom-axis labels at local midnights, at most 6."""
    if not xs:
        return []
    first = dt.datetime.fromtimestamp(min(xs), TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    d = first
    while d.timestamp() <= max(xs):
        days.append(d)
        d += dt.timedelta(days=1)
    step = max(1, len(days) // 6 + (1 if len(days) % 6 else 0))
    return [(d.timestamp(), f"{d.day} {d:%b}") for d in days[::step]]


@app.get("/bg-taste-match", response_class=HTMLResponse)
def bgt_page(request: Request):
    prog, now = bgt.progress.get(), bgt.now.get()
    history = store.bgt_history()
    chart = None
    if len(history) > 1:
        xs = [h["at"] for h in history]
        chart = charts.lines([{"name": label, "slot": i + 1, "points": [(h["at"], h.get(key)) for h in history]}
                              for i, (key, label) in enumerate(bgt.STAGES[:4])],
                             title="Videos per stage, by day", x_labels=day_labels(xs))
    # Whisper's speed on the GPU (audio minutes per minute of work) and the failures, from the passes' logs:
    # the running ones live, the finished ones as the lab saved them.
    saved = store.job_logs("bg-taste-match", days=7) + store.job_logs("bgt-", days=7)
    live = [r for r in (now.data or {}).get("runs", []) if r["state"] == "running"]
    audio = took = 0.0
    fails = []
    for run in [{"name": r["name"], "start": r["start"], "summary": r["log"]} for r in live] + saved:
        for s in run["summary"].get("stages", []):
            audio += s.get("audio_min", 0)
            took += s.get("took_min", 0)
            fails += [{"line": f, "run": run["name"], "at": run.get("start")} for f in s.get("fails", [])]
    return page(request, "bgt.html", prog=prog, pipe=now, activity=bgt.activity.get(), problems=bgt.problems.get(),
                progress_chart=chart, has_results=bool(bgt.results_file()), fails=fails,
                whisper_speed=(audio / took) if took > 30 else None,
                runs=store.job_history(prefix="bg-taste-match", days=7))


@app.get("/bg-taste-match/census")
def bgt_census():
    path = os.path.join(bgt.BGT_DIR, "census.html")
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="text/html")


@app.get("/bg-taste-match/v/{vid}", response_class=HTMLResponse)
def bgt_video(request: Request, vid: str):
    if not re.fullmatch(r"[\w-]{6,20}", vid):
        raise HTTPException(404)
    r = bgt.video.get(vid)
    if r.ok and r.data is None:
        raise HTTPException(404)
    return page(request, "bgt_video.html", r=r, names=bgt.channel_names.get().data or {})


@app.get("/bg-taste-match/search", response_class=HTMLResponse)
def bgt_search(request: Request, q: str = ""):
    return page(request, "bgt_search.html", q=q, r=bgt.search.get(q[:80]), names=bgt.channel_names.get().data or {})


@app.get("/bg-taste-match/results")
def bgt_results():
    path = bgt.results_file()
    if not path:
        raise HTTPException(404, "No results page built yet")
    return FileResponse(path, media_type="text/html")


# ---- deals (BoardGameFinder) ------------------------------------------------------------------------------------

@app.get("/deals", response_class=HTMLResponse)
def deals_page(request: Request):
    market = bgf.market.get()
    points = []
    for i, (day, n) in enumerate((market.data or {}).get("per_day", [])):
        d = dt.date.fromisoformat(day)
        points.append({"value": n or 0, "label": f"{d.day} {d:%b}" if i % 7 == 1 else "",
                       "tip": f"{d.day} {d:%b}: " + (f"{n:,} listings" if n else "no scrape")})
    return page(request, "deals.html", deals=bgf.deals.get(), market=market, run=bgf.run.get(),
                per_day_points=points, history=store.job_history("boardgamefinder", days=30))


@app.get("/deals/review-packet")
def deals_review_packet():
    path = os.path.join(bgf.BGF_DIR, "Output", "review_packet.md")
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="text/plain; charset=utf-8")


@app.get("/deals/game/{bg_id}", response_class=HTMLResponse)
def deals_game(request: Request, bg_id: int):
    r = bgf.game.get(bg_id)
    chart = None
    if r.ok and r.data and r.data["history"]:
        pts = [(dt.datetime.fromisoformat(h["day"]).replace(tzinfo=TZ).timestamp(), h["low"]) for h in r.data["history"]]
        chart = charts.lines([{"name": "Cheapest ask", "slot": 1, "points": pts}], title="Cheapest ask per day",
                             x_labels=day_labels([p[0] for p in pts]), unit=" €", step=True)
    return page(request, "deals_game.html", r=r, price_chart=chart)


@app.get("/deals/digest")
def deals_digest():
    path = os.path.join(bgf.BGF_DIR, "Output", "deals.html")
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="text/html")


# ---- cats -------------------------------------------------------------------------------------------------------

@app.get("/cats", response_class=HTMLResponse)
def cats_page(request: Request, at: str = ""):
    ev = cats.events.get()
    evts = ev.data or []
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
    per_day = cats.per_day(evts)
    day_points = [{"value": n, "label": f"{d.day}" if i % 2 == 1 else "", "tip": f"{d.day} {d:%b}: {n} visits"}
                  for i, (d, n) in enumerate(per_day)]
    hours = cats.per_hour(evts)
    hour_points = [{"value": n, "label": f"{h}h" if h % 6 == 0 else "", "tip": f"{h}:00–{h}:59: {n} visits"}
                   for h, n in enumerate(hours)]
    w = cats.weights.get()
    weight_chart = None
    if w.ok and w.data:
        since = now_ts - 400 * 86400
        series = [{"name": c, "slot": i + 1, "points": [p for p in pts if p[0] >= since]}
                  for i, (c, pts) in enumerate(sorted(w.data.items()))]
        xs = [p[0] for s in series for p in s["points"]]
        if xs:
            lo = dt.datetime.fromtimestamp(min(xs), TZ)
            months = []
            m = lo.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            while m.timestamp() <= max(xs):
                months.append(m)
                m = (m + dt.timedelta(days=32)).replace(day=1)
            step = max(1, (len(months) + 5) // 6)
            weight_chart = charts.lines(series, title="Weight, kg", unit=" kg",
                                        x_labels=[(m.timestamp(), f"{m:%b}") for m in months[::step]])
    return page(request, "cats.html", events=ev, day_points=day_points, hour_points=hour_points,
                monitor=cats.monitor.get(), weights=w, weight_chart=weight_chart, frames=frames, look=look,
                at=at, runs=store.job_history("cat-monitor", days=30), now_ts=now_ts,
                cron=((k8s.jobs.get().data or {}).get("cronjobs") or {}).get("cat-monitor"))


_frigate = httpx.AsyncClient(timeout=httpx.Timeout(10, read=120))
EVENT_ID = re.compile(r"\d+\.\d+-[a-z0-9]+")


async def _proxy(url, params=None, media="image/jpeg", cache=300):
    req = _frigate.build_request("GET", url, params=params)
    try:
        r = await _frigate.send(req, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Frigate: {type(e).__name__}")
    if r.status_code != 200:
        await r.aclose()
        raise HTTPException(r.status_code if r.status_code < 500 else 502, "Frigate has no such image or clip")
    headers = {"Cache-Control": f"private, max-age={cache}"}
    if "content-length" in r.headers:
        headers["Content-Length"] = r.headers["content-length"]
    return StreamingResponse(r.aiter_raw(), media_type=r.headers.get("content-type", media), headers=headers,
                             background=None if False else _Closer(r))


class _Closer:
    def __init__(self, r):
        self.r = r

    async def __call__(self):
        await self.r.aclose()


CLIPS = os.path.join(os.path.dirname(store.LAB_DB) or ".", "clips")
CLIPS_MAX_BYTES = 400 * 2**20


async def _clip(url, name):
    """A clip saved to disk first (streamed, never held in memory), then served with Range support: iPhones only
    play video they can seek in. Old clips are dropped past 2 hours or 400 MB."""
    os.makedirs(CLIPS, exist_ok=True)
    path = os.path.join(CLIPS, name)
    if not os.path.exists(path):
        files = sorted((os.path.join(CLIPS, f) for f in os.listdir(CLIPS)), key=os.path.getmtime)
        total = sum(os.path.getsize(f) for f in files)
        for f in files:
            if time.time() - os.path.getmtime(f) > 7200 or total > CLIPS_MAX_BYTES:
                total -= os.path.getsize(f)
                os.remove(f)
        tmp = f"{path}.{uuid.uuid4().hex}.part"     # two viewers opening the same clip don't share a file
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


@app.get("/cats/media/event/{eid}/snapshot.jpg")
async def cat_event_snapshot(eid: str):
    if not EVENT_ID.fullmatch(eid):
        raise HTTPException(404)
    return await _proxy(cats.event_snapshot_url(eid), {"bbox": 1, "height": 360}, cache=86400)


@app.get("/cats/media/event/{eid}/thumbnail.jpg")
async def cat_event_thumbnail(eid: str):
    if not EVENT_ID.fullmatch(eid):
        raise HTTPException(404)
    return await _proxy(f"{cats.FRIGATE}/api/events/{eid}/thumbnail.jpg", cache=86400)


@app.get("/cats/media/event/{eid}/clip.mp4")
async def cat_event_clip(eid: str):
    if not EVENT_ID.fullmatch(eid):
        raise HTTPException(404)
    return await _clip(cats.event_clip_url(eid), f"event-{eid}.mp4")


@app.get("/cats/media/frame/{ts}.jpg")
async def cat_frame(ts: int):
    if not 0 < time.time() - ts < 15 * 86400:
        raise HTTPException(404)
    return await _proxy(cats.frame_url(ts), {"height": 360}, cache=86400)


@app.get("/cats/media/latest.jpg")
async def cat_latest():
    return await _proxy(cats.latest_url(), {"height": 480}, cache=5)


@app.get("/cats/media/clip/{start}-{end}.mp4")
async def cat_clip(start: int, end: int):
    if not (0 < end - start <= 600 and 0 < time.time() - start < 15 * 86400):
        raise HTTPException(400, "Clips are up to 10 minutes from the last 14 days")
    return await _clip(cats.range_clip_url(start, end), f"range-{start}-{end}.mp4")


# ---- GPU and PC -------------------------------------------------------------------------------------------------

@app.get("/gpu", response_class=HTMLResponse)
def gpu_page(request: Request):
    from .overview import gpu_users
    now_h = int(time.time()) // 3600 * 3600
    hour_labels = [(t, dt.datetime.fromtimestamp(t, TZ).strftime("%H:%M")) for t in range(now_h - 24 * 3600, now_h + 1, 6 * 3600)]
    return page(request, "gpu.html", gpu=prom.gpu.get(), hist=prom.gpu_history.get(24), users=gpu_users(),
                hour_labels=hour_labels,
                jobs=k8s.jobs.get(), runs=store.job_history(prefix="bg-taste-match-gpu", days=7)
                + store.job_history("cat-monitor", days=7))


@app.get("/pc", response_class=HTMLResponse)
def pc_page(request: Request):
    return page(request, "pc.html", r=misc.pc.get())


# ---- report intake (port 8081 only) -----------------------------------------------------------------------------

intake = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@intake.post("/report/{name}")
async def report(name: str, request: Request):
    if name not in ("pc",):
        raise HTTPException(404)
    token = os.environ.get("INTAKE_TOKEN", "")
    if not token or not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
        raise HTTPException(401)
    body = await request.body()
    if len(body) > 256 * 1024:
        raise HTTPException(413)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON expected")
    store.add_report(name, data)
    return {"ok": True}


@intake.get("/healthz")
def intake_health():
    return {"ok": True}


# ---- background: remember job runs and the daily pipeline snapshot ----------------------------------------------

def poll_once():
    j = k8s.jobs.get()
    if j.ok:
        store.record_jobs(j.data["jobs"])
        # Keep each finished run's log summary before Kubernetes deletes the Job (it keeps only the last few).
        saved = store.logged_uids()
        for job in j.data["jobs"]:
            if job["state"] in ("ok", "failed") and job["uid"] not in saved:
                pods = k8s.pods_of_job(job["name"])
                text = k8s.get(f"/api/v1/namespaces/{job['namespace']}/pods/{pods[0]['name']}/log",
                               tailLines=5000) if pods else ""
                kind = job["cronjob"] or job["name"]
                store.save_log(job, logs.summarize(kind, text if isinstance(text, str) else ""),
                               logs.tail(text if isinstance(text, str) else "", 60))
    p = bgt.progress.get()
    if p.ok:
        store.record_bgt(p.data["totals"])


async def poller():
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:
            log.exception("poll failed")
        await asyncio.sleep(120)


async def serve():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store.init()
    pages = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)),
                                          proxy_headers=True, forwarded_allow_ips="*", access_log=False))
    reports = uvicorn.Server(uvicorn.Config(intake, host="0.0.0.0", port=int(os.environ.get("INTAKE_PORT", 8081)),
                                            access_log=False))
    pages.install_signal_handlers = reports.install_signal_handlers = lambda: None
    task = asyncio.create_task(poller())
    try:
        await asyncio.gather(pages.serve(), reports.serve())
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(serve())
