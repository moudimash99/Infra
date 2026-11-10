"""What the three sites share: the design (templates/base.html, static/lab.css), the page helper, icons, axis labels.

Each site answers at its own address (tastematch., deals., catcam.machaka.net) at "/"; on a development machine
they sit under /tastematch, /deals and /catcam of one address. Links in templates start with R, the site's root
("" at its own address), so the same templates work both ways.

A site is made with site(key, nav): nav is its sections, [(path, label, icon)], shown as tabs at the bottom of a
phone and in the header from 900 px. page() renders a template with the site, the active section and R.
"""
import datetime as dt
import hashlib
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import charts, fmt
from .config import TZ

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
# Changes with the files, so browsers fetch new stylesheets, scripts and icons after a deploy (a site's own
# static/<site>.css or .js included).
STATIC_V = hashlib.sha1(b"".join(open(os.path.join(STATIC, f), "rb").read() for f in sorted(os.listdir(STATIC))
                                 if f.endswith((".css", ".js", ".svg")))).hexdigest()[:8]

SITES = {
    "tastematch": {"key": "tastematch", "name": "Taste Match", "url": "https://tastematch.machaka.net",
                   "tagline": "Reviewers who share your taste"},
    "deals": {"key": "deals", "name": "Deals", "url": "https://deals.machaka.net",
              "tagline": "Board game bargains on Okkazeo"},
    "catcam": {"key": "catcam", "name": "Cat cam", "url": "https://catcam.machaka.net",
               "tagline": "Simba and Nala, day by day"},
}


def icon(name, cls=""):
    """An icon from static/icons.svg, in the text's colour."""
    extra = f" {escape(cls)}" if cls else ""
    return Markup(f'<svg class="i{extra}" aria-hidden="true"><use href="/static/icons.svg?v={STATIC_V}#{escape(name)}"/></svg>')


templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
fmt.register(templates.env)
templates.env.globals.update(charts=charts, icon=icon, STATIC_V=STATIC_V, LAB_URL="https://lab.machaka.net")


def site(key, nav):
    """A FastAPI app for one site, with the shared stylesheet, script and icons under /static."""
    app = FastAPI(title=SITES[key]["name"], docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.site = {**SITES[key], "nav": nav}
    return app


def page(request, name, active=None, **ctx):
    """Render a template for the site that got the request. active: the nav path to highlight (default: the
    section the URL is in)."""
    site_ = request.app.state.site
    root = request.scope.get("root_path", "").rstrip("/")
    path = request.url.path
    rel = (path[len(root):] if root and path.startswith(root) else path) or "/"
    if active is None:
        active = next((h for h, _, _ in sorted(site_["nav"], key=lambda n: -len(n[0]))
                       if rel == h or (h != "/" and rel.startswith(h.rstrip("/") + "/"))), None)
    sites = [{**s, "url": f"/{s['key']}/" if root else s["url"]} for s in SITES.values()]
    return templates.TemplateResponse(request, name, {"R": root, "site": site_, "me": site_["key"], "active": active,
                                                      "SITES": sites, "path": path, **ctx})


def day_labels(xs, most=6):
    """Bottom-axis labels at local midnights, at most `most`."""
    if not xs:
        return []
    d = dt.datetime.fromtimestamp(min(xs), TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    while d.timestamp() <= max(xs):
        days.append(d)
        d += dt.timedelta(days=1)
    step = max(1, -(-len(days) // most))
    return [(d.timestamp(), f"{d.day} {d:%b}") for d in days[::step]]
