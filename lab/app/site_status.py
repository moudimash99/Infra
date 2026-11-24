"""The homelab tool at lab.machaka.net/machines: Grafana's dashboards, regrouped by question.

  /         Now: what is happening (machines, jobs, who waits for room, GPU, sites, the PC)
  /jobs     every scheduled job and PC task: its state, its last 7 days, how long runs take, memory against the limit
  /memory   where each machine's memory goes, and everything using it

The bell in the header lists the alerts firing now: the Prometheus alert rules (k8s/monitoring/alert-rules.yml),
the same ones that email the owner, nothing worked out here. Same look and root handling as the other lab sites
(web.page); the colours and icons are Machines'.
"""
import datetime as dt

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import site_machines
from .sources import jobruns, overview, prom
from .config import TZ
from .web import SITES, STATIC, day_labels, page

NAV = [("/", "Now", "gauge"), ("/jobs", "Jobs", "calendar"), ("/memory", "Memory", "layers")]
MACHINES = [("moudimash99", "Server"), ("acer-laptop", "Acer"), ("pc", "PC")]
RANGES = {"24h": 24, "7d": 168}

app = FastAPI(title="Homelab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.state.site = {**SITES["machines"], "name": "Homelab", "tagline": "Is everything all right, and why", "nav": NAV}

STATE_WORDS = {"bad": "Needs a look", "warn": "Watch", "ok": "Fine", "running": "Running", "paused": "Paused",
               "info": "Note", "unknown": "Not checked"}
# Where an alert sends you: by the start of its rule's name (alert-rules.yml).
ALERT_PAGES = [(("Job", "BoardGameFinder", "CatMonitor", "Backups", "BgTasteMatch"), "/jobs"),
               (("Node", "GPU", "ContainerMemory", "ContainerOOM"), "/memory"),
               (("Pod", "Container", "Deployment", "DaemonSet"), "/#gets-in")]


def alarms(request):
    """The firing, unsilenced alerts, for the bell: [{name, summary, state, since, href}]."""
    r = prom.alerts.get()
    root = request.scope.get("root_path", "").rstrip("/")
    out = []
    for a in r.data or []:
        href = next((path for starts, path in ALERT_PAGES if a["name"].startswith(starts)), "/")
        out.append({"name": a["name"], "summary": a.get("summary") or a["name"],
                    "state": "bad" if a.get("severity") == "critical" else "warn",
                    "since": overview._ts(a.get("since")), "href": root + href})
    return {"list": out, "ok": r.ok}


def common(request):
    return {"alarms": alarms(request), "MACHINES": MACHINES, "W": STATE_WORDS, "dur": overview.dur,
            "size": overview.size, "clock": overview.clock, "mname": overview.machine_name}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    r = overview.now.get()
    return page(request, "status/overview.html", reading=r, d=r.data, **common(request))


@app.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, range: str = "24h"):
    hours = RANGES.get(range, 24)
    r, w = overview.now.get(), jobruns.week.get()
    graphs = jobruns.graphs(w.data, hours) if w.data else {"took": [], "mem": []}
    day_names = [f"{dt.datetime.fromtimestamp(s, TZ):%a}"[:2] for s in w.data["starts"]] if w.data else []
    return page(request, "status/jobs.html", reading=r, week=w, d=r.data, w=w.data, graphs=graphs, range=range,
                RANGES=RANGES, hours=hours, day_names=day_names, x_labels=axis(hours), **common(request))


def axis(hours, now=None):
    """Bottom-axis labels: every 6 hours over a day, local midnights over a week."""
    import time
    now = now or time.time()
    if hours > 48:
        return day_labels([now - hours * 3600, now], most=7)
    start = dt.datetime.fromtimestamp(now - hours * 3600, TZ).replace(minute=0, second=0, microsecond=0)
    out = []
    while start.timestamp() <= now:
        if start.hour % 6 == 0:
            out.append((start.timestamp(), f"{start:%H:%M}"))
        start += dt.timedelta(hours=1)
    return out


@app.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request):
    return site_machines.render(request, **common(request))
