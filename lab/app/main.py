"""Lab: three small sites, each at its own address, served by one process.

  tastematch.machaka.net  the review pipeline (site_bgt)
  deals.machaka.net       BoardGameFinder's deals (site_deals)
  catcam.machaka.net      the cats: flash of the day, camera, weights (site_cats)

Each checks the Cloudflare Access login itself (access.py). lab.machaka.net is Homepage; its tiles read short numbers
from /api/summary/* here, inside the cluster. On a development machine the sites sit under /tastematch, /deals and
/catcam. Port 8081 (NodePort, home network only) takes the PC's reports and serves them as Prometheus metrics for
Grafana.
"""
import asyncio
import datetime as dt
import logging
import os
import re
import secrets
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.applications import Starlette
from starlette.routing import Host, Mount, Route
from starlette.responses import JSONResponse

from . import logs, store, summary
from .access import AccessGate
from .sources import bgt, k8s
from . import site_bgt, site_cats, site_deals

log = logging.getLogger("lab")
SITES = [("tastematch", os.environ.get("HOST_TASTEMATCH", "tastematch.machaka.net"), site_bgt.app, "Taste Match"),
         ("deals", os.environ.get("HOST_DEALS", "deals.machaka.net"), site_deals.app, "Deals"),
         ("catcam", os.environ.get("HOST_CATCAM", "catcam.machaka.net"), site_cats.app, "Cat cam")]

# ---- in-cluster API for Homepage -------------------------------------------------------------------------------

api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
SUMMARIES = {"tastematch": summary.taste_match, "deals": summary.deals, "catcam": summary.catcam}


@api.get("/summary/{name}")
def api_summary(name: str):
    if name not in SUMMARIES:
        raise HTTPException(404)
    return SUMMARIES[name]()


async def healthz(request):
    return JSONResponse({"ok": True})


async def dev_index(request):
    links = "".join(f'<li><a href="/{key}/">{title}</a> (at {host})</li>' for key, host, _, title in SITES)
    return HTMLResponse(f"<!DOCTYPE html><title>Lab (development)</title><ul>{links}</ul>")


app = AccessGate(Starlette(routes=[
    *[Host(host, app=sub) for _, host, sub, _ in SITES],
    Route("/healthz", healthz),
    Mount("/api", app=api),
    *[Mount(f"/{key}", app=sub) for key, _, sub, _ in SITES],
    Route("/", dev_index),
]))

# ---- the PC's reports (port 8081 only) -----------------------------------------------------------------------

intake = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@intake.post("/report/{name}")
async def report(name: str, request: Request):
    if name != "pc":
        raise HTTPException(404)
    token = os.environ.get("INTAKE_TOKEN", "")
    if not token or not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
        raise HTTPException(401)
    if len(await request.body()) > 256 * 1024:
        raise HTTPException(413)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON expected")
    await asyncio.to_thread(store.add_report, name, data)
    return {"ok": True}


def _label(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:120]


def _num(v):
    try:
        return float(str(v).replace(",", "").split()[0])
    except (ValueError, IndexError):
        return None


@intake.get("/metrics", response_class=PlainTextResponse)
def pc_metrics():
    """The PC's last report as Prometheus metrics (Grafana's PC dashboard reads these)."""
    rep = store.latest_report("pc")
    if not rep:
        return "# no report from the PC yet\n"
    body, lines = rep["body"], []
    lines.append("# HELP pc_report_timestamp_seconds When the PC last reported (every 10 minutes while it is on).")
    lines.append("# TYPE pc_report_timestamp_seconds gauge")
    lines.append(f"pc_report_timestamp_seconds {rep['received']:.0f}")
    lines.append("# HELP pc_task_ok 1 when a scheduled task's last result is fine (ok or still running), 0 when it failed.")
    lines.append("# TYPE pc_task_ok gauge")
    for t in body.get("tasks", []):
        task = _label(t.get("name", "?"))
        state = t.get("status", "unknown")
        lines.append(f'pc_task_ok{{task="{task}",status="{_label(state)}",note="{_label(t.get("note", ""))}"}} '
                     f'{0 if state == "failed" else 1}')
        for key, metric in (("last_run", "pc_task_last_run_timestamp_seconds"), ("next_run", "pc_task_next_run_timestamp_seconds")):
            if t.get(key):
                try:
                    ts = dt.datetime.fromisoformat(t[key]).timestamp()
                    lines.append(f'{metric}{{task="{task}"}} {ts:.0f}')
                except ValueError:
                    pass
    lines.append("# HELP pc_job_value A number from a PC job's own log (archived today, jobs found, inbox items...).")
    lines.append("# TYPE pc_job_value gauge")
    for job, info in (body.get("jobs") or {}).items():
        for name, value in (info.get("numbers") or {}).items():
            n = _num(value)
            if n is not None and not re.search(r"\d{1,2}:\d\d", str(value)):   # times aren't numbers
                lines.append(f'pc_job_value{{job="{_label(job)}",title="{_label(info.get("title", job))}",'
                             f'name="{_label(name.strip())}"}} {n:g}')
        lines.append(f'pc_job_ok{{job="{_label(job)}",title="{_label(info.get("title", job))}"}} '
                     f'{0 if info.get("status") == "failed" else 1}')
    return "\n".join(lines) + "\n"


@intake.get("/healthz")
def intake_health():
    return {"ok": True}


# ---- background: remember job runs and the daily pipeline snapshot ----------------------------------------------

def poll_once():
    p = bgt.progress.get()
    if p.ok:
        store.record_bgt(p.data["totals"])
    j = k8s.jobs.get()
    if not j.ok:
        return
    jobs = [dict(x) for x in j.data["jobs"]]
    for job in jobs:   # a failed run's real cause is its pod's (OOMKilled, Error), not the Job's BackoffLimitExceeded
        if job["state"] == "failed":
            pod = k8s.latest_pod(job["name"])
            if pod and pod.get("reason"):
                job["reason"] = pod["reason"]
    store.record_jobs(jobs)
    # Keep each finished run's log summary before Kubernetes deletes the Job (it keeps only the last few).
    saved = store.logged_uids()
    for job in jobs:
        if job["state"] not in ("ok", "failed") or job["uid"] in saved:
            continue
        text = ""
        try:
            pod = k8s.latest_pod(job["name"])
            if pod:
                got = k8s.get(f"/api/v1/namespaces/{job['namespace']}/pods/{pod['name']}/log", tailLines=5000)
                text = got if isinstance(got, str) else ""
        except Exception as e:     # a log that's gone or unreachable: save what we know, don't retry forever
            log.warning("log of %s unreadable: %s", job["name"], e)
        store.save_log(job, logs.summarize(job["cronjob"] or job["name"], text), logs.tail(text, 60))


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
