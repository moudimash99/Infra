"""The homelab tool at lab.machaka.net/machines: Grafana's dashboards, regrouped by question.

  /         Now: what is happening (machines, jobs, who waits for room, GPU, sites, the PC)
  /jobs     every scheduled job and PC task: its state, its last 7 days, how long runs take, memory against the limit
  /server   one machine (also /acer): right now, where its memory goes, what uses it, CPU, pressure, temperatures,
            disks, network, the GPU (Acer), Docker (server), with graphs over 24 h or 7 d
  /pc       the PC: on or asleep, its scheduled tasks and their last 7 days, what its jobs reported
  /apps     every app and job in Kubernetes: room on each machine, rank, booking and real memory, restarts and why,
            its last 24 hours; who waits for room; the ranks
  /network  every address Traefik serves (up, requests, failures, speed), traffic in and out of each machine, the
            apps that moved the most
  /apps/<namespace>/<app>   one app: right now, memory against booking and limit, CPU, network, restarts, its log
  /alarms   every alert rule: firing, about to fire or quiet, what it watches, its last 7 days; what is muted
            (opened from the bell, not the tabs)
  /memory   (old address) sends you to the machine's page

The bell in the header lists the alerts firing now: the Prometheus alert rules (k8s/monitoring/alert-rules.yml),
the same ones that email the owner, nothing worked out here. Same look and root handling as the other lab sites
(web.page); the colours and icons are Machines'.
"""
import datetime as dt

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import site_machines
from .sources import alarms as alarm_rules, appdetail, apps, hosts, jobruns, memory, net, overview, prom
from .config import TZ
from .web import SITES, STATIC, day_labels, page

NAV = [("/", "Now", "gauge"), ("/jobs", "Jobs", "calendar"), ("/server", "Machines", "cpu"), ("/apps", "Apps", "package"),
       ("/network", "Network", "activity")]
MACHINES = [("moudimash99", "Server"), ("acer-laptop", "Acer"), ("pc", "PC")]
RANGES = {"24h": 24, "7d": 168}

app = FastAPI(title="Homelab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.state.site = {**SITES["machines"], "name": "Homelab", "tagline": "Is everything all right, and why", "nav": NAV}

STATE_WORDS = {"bad": "Needs a look", "warn": "Watch", "ok": "Fine", "running": "Running", "paused": "Paused",
               "info": "Note", "unknown": "Not checked"}
# Where an alert sends you: by the start of its rule's name (alert-rules.yml); a machine's alerts go to its page.
ALERT_PAGES = [(("Job", "BoardGameFinder", "CatMonitor", "Backups", "BgTasteMatch"), "/jobs"),
               (("GPU",), "/acer#gpu"),
               (("PodPending",), "/apps#gets-in"),
               (("Pod", "Container", "Deployment", "DaemonSet"), "/apps")]
NODE_PAGES = {"NodeHot": "#temps", "NodeVeryHot": "#temps", "NodeDisk": "#disks", "NodeFilesystem": "#disks",
              "NodeMemory": "#memory", "NodeOOM": "#memory", "NodeCPU": "#cpu", "NodeLoad": "#struggling"}


def alarms(request):
    """The firing, unsilenced alerts, for the bell: [{name, summary, state, since, href}]."""
    r = prom.alerts.get()
    root = request.scope.get("root_path", "").rstrip("/")
    out = []
    for a in r.data or []:
        href = next((path for starts, path in ALERT_PAGES if a["name"].startswith(starts)), "/")
        if a["name"].startswith("Node"):
            lab = a.get("labels") or {}
            node = lab.get("node") or lab.get("instance", "").split(":")[0]
            slug = hosts.NODE_SLUG.get(node)
            if slug:
                href = "/" + slug + next((v for k, v in NODE_PAGES.items() if a["name"].startswith(k)), "")
        out.append({"name": a["name"], "summary": a.get("summary") or a["name"],
                    "state": "bad" if a.get("severity") == "critical" else "warn",
                    "since": overview._ts(a.get("since")), "href": root + href})
    return {"list": out, "ok": r.ok}


def rate(v):
    """Bytes a second in words: 120 KB/s, 3.4 MB/s."""
    if v is None:
        return "?"
    return f"{v / 1024 ** 2:.1f} MB/s" if v >= 1024 ** 2 else f"{v / 1024:.0f} KB/s"


def common(request):
    return {"alarms": alarms(request), "MACHINES": MACHINES, "W": STATE_WORDS, "dur": overview.dur,
            "size": overview.size, "clock": overview.clock, "mname": overview.machine_name, "rate": rate,
            "SLUG": hosts.NODE_SLUG}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    r = overview.now.get()
    return page(request, "status/overview.html", reading=r, d=r.data, **common(request))


@app.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, range: str = "24h"):
    range = range if range in RANGES else "24h"
    hours = RANGES[range]
    r, w, mem = overview.now.get(), jobruns.week.get(), jobruns.memory.get(range)
    graphs = jobruns.graphs(w.data, hours) if w.data else {"took": [], "mem": []}
    per = jobruns.per_job(w.data, mem.data, hours) if w.data else {}
    day_names = [f"{dt.datetime.fromtimestamp(s, TZ):%a}"[:2] for s in w.data["starts"]] if w.data else []
    return page(request, "status/jobs.html", reading=r, week=w, d=r.data, w=w.data, graphs=graphs, per=per, range=range,
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


def machine(request, slug, range):
    node = hosts.SLUGS[slug]
    range = range if range in hosts.RANGES else "24h"
    r, mem = hosts.read.get(node, range), memory.now.get()
    d = hosts.build(node, r.data, mem.data, overview.load_catalog(), range) if r.data else None
    extra = {}
    if d and d["machine"]:
        state, _ = memory.verdict(d["machine"])
        top = d["machine"]["top"]
        low = memory.low.get(node)
        extra = {"mstate": state, "low": low.data if low.ok else None,
                 "msentence": ("The biggest users: " + ", ".join(f"{t['name']} {overview.size(t['now'])}" for t in top)
                               + ".") if top else ""}
    if d:
        fullest = max(d["disks"], key=lambda x: x["pct"]) if d["disks"] else None
        hottest = max(d["sensors"], key=lambda x: x["now"]) if d["sensors"] else None
        extra.update(fullest_days=fullest and fullest["days"], hot_peak=hottest and hottest["peak"])
    return page(request, "status/machine.html", active="/server", reading=r, d=d, range=range, RANGES=hosts.RANGES,
                x_labels=axis(hosts.RANGES[range][0]), PARTS=site_machines.PARTS, KINDS=site_machines.KINDS,
                spark=site_machines.spark, G=memory.G, **{"low": None, "mstate": "ok", "msentence": "", **extra},
                **common(request))


@app.get("/server", response_class=HTMLResponse)
def server_page(request: Request, range: str = "24h"):
    return machine(request, "server", range)


@app.get("/acer", response_class=HTMLResponse)
def acer_page(request: Request, range: str = "24h"):
    return machine(request, "acer", range)


@app.get("/pc", response_class=HTMLResponse)
def pc_page(request: Request):
    r, w = overview.now.get(), jobruns.week.get()
    return page(request, "status/pc.html", active="/server", reading=r, d=r.data, w=w.data, **common(request))


@app.get("/apps", response_class=HTMLResponse)
def apps_page(request: Request, range: str = "24h"):
    range = range if range in apps.RANGES else "24h"
    r, mem, ov = apps.read.get(range), memory.now.get(), overview.now.get()
    a = apps.build(r.data, mem.data, overview.load_catalog(), range_key=range) if r.data else None
    return page(request, "status/apps.html", reading=r, a=a, d=ov.data, range=range, RANGES=apps.RANGES,
                groups=a["groups"] if a else [], with_search=True, x_labels=axis(apps.RANGES[range][0]),
                **{**common(request), "MACHINES": MACHINES[:2]})


@app.get("/apps/{ns}/{name}", response_class=HTMLResponse)
def app_page(request: Request, ns: str, name: str, range: str = "24h"):
    range = range if range in appdetail.RANGES else "24h"
    listing, mem = apps.read.get("24h"), memory.now.get()
    catalog = overview.load_catalog()
    a = apps.build(listing.data, mem.data, catalog) if listing.data else None
    rows = [r for r in (a["rows"] if a else []) if r["ns"] == ns and r["name"] == name]
    rank = next((k for k in catalog.get("ranks", []) if rows and k["name"] == rows[0]["rank"]), None)
    r = appdetail.read.get(ns, name, range)
    live = sorted({p for x in rows for p in x["live"]})
    return page(request, "status/app.html", active="/apps", reading=r, ns=ns, app_name=name, rows=rows,
                r=rows[0] if rows else None, rank=rank, g=appdetail.graphs(r.data or {}, name),
                log=appdetail.log_of(ns, live), range=range, RANGES=appdetail.RANGES,
                x_labels=axis(appdetail.RANGES[range][0]), **common(request))


@app.get("/network", response_class=HTMLResponse)
def network_page(request: Request, range: str = "24h"):
    range = range if range in net.RANGES else "24h"
    r = net.read.get(range)
    n = net.build(r.data, overview.load_catalog(), range) if r.data else None
    return page(request, "status/network.html", reading=r, n=n, d=None, range=range, RANGES=net.RANGES,
                groups=n["groups"] if n else [], x_labels=axis(net.RANGES[range][0]), **common(request))


@app.get("/alarms", response_class=HTMLResponse)
def alarms_page(request: Request):
    r = alarm_rules.read.get()
    x = alarm_rules.build(r.data) if r.data else None
    names = [f"{dt.datetime.fromtimestamp(s, TZ):%a}"[:2] for s in x["starts"]] if x else []
    return page(request, "status/alarms.html", active=None, reading=r, x=x, day_names=names, **common(request))


@app.get("/memory")
def memory_page(request: Request, m: str = ""):
    """The old memory page lives on each machine's page now."""
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(f"{root}/{hosts.NODE_SLUG.get(m, 'server')}#memory", status_code=302)
