"""The homelab overview (to replace Machines at lab.machaka.net/machines once reviewed).

  /         Overview: every question the Grafana dashboards answer, answered in plain words with the reason, a few
            lines each, filtered by machine, project and "problems only" (sources/overview.py)
  /memory   where each machine's memory goes, and everything using it (the Machines page)

Same look and root handling as the other lab sites (web.page); the site's colours and icons are Machines'.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import site_machines
from .sources import overview
from .web import SITES, STATIC, page

NAV = [("/", "Overview", "gauge"), ("/memory", "Memory", "layers")]
MACHINES = [("moudimash99", "Server"), ("acer-laptop", "Acer"), ("pc", "PC")]

app = FastAPI(title="Homelab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.state.site = {**SITES["machines"], "name": "Homelab", "tagline": "Is everything all right, and why", "nav": NAV}

STATE_WORDS = {"bad": "Needs a look", "warn": "Watch", "ok": "Fine", "running": "Running", "paused": "Paused",
               "info": "Note", "unknown": "Not checked"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    r = overview.now.get()
    return page(request, "status/overview.html", reading=r, d=r.data, MACHINES=MACHINES, W=STATE_WORDS,
                dur=overview.dur, size=overview.size, clock=overview.clock, mname=overview.machine_name)


@app.get("/memory", response_class=HTMLResponse)
def memory_page(request: Request):
    return site_machines.home(request)
