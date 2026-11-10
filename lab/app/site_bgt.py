"""tastematch.machaka.net: where the review pipeline is, by channel, and what it found."""
import os
import re

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import charts, store
from .sources import bgt
from .web import day_labels, page, site, templates

templates.env.globals.update(STAGES=bgt.STAGES)
app = site("Taste Match")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    prog, now = bgt.progress.get(), bgt.now.get()
    history = store.bgt_history()
    chart = None
    if len(history) > 1:
        chart = charts.lines([{"name": label, "slot": i + 1, "points": [(h["at"], h.get(key)) for h in history]}
                              for i, (key, label) in enumerate(bgt.STAGES[:4])],
                             title="Videos per stage, by day", x_labels=day_labels([h["at"] for h in history]))
    # Whisper's speed on the GPU (audio minutes per minute of work) and the failures, from the passes' logs:
    # the running ones live, the finished ones as the lab saved them (one entry per run).
    saved = list({r["uid"]: r for r in store.job_logs("bg-taste-match", days=7) + store.job_logs("bgt-", days=7)}.values())
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


@app.get("/v/{vid}", response_class=HTMLResponse)
def video(request: Request, vid: str):
    if not re.fullmatch(r"[\w-]{6,20}", vid):
        raise HTTPException(404)
    r = bgt.video.get(vid)
    if r.ok and r.data is None:
        raise HTTPException(404)
    return page(request, "bgt_video.html", r=r, names=bgt.channel_names.get().data or {})


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    return page(request, "bgt_search.html", q=q, r=bgt.search.get(q[:80]), names=bgt.channel_names.get().data or {})


@app.get("/results")
def results():
    path = bgt.results_file()
    if not path:
        raise HTTPException(404, "No results page built yet")
    return FileResponse(path, media_type="text/html")


@app.get("/census")
def census():
    path = os.path.join(bgt.BGT_DIR, "census.html")
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="text/html")
