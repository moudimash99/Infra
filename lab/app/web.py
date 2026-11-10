"""What the three sites share: templates, the page helper, the stylesheet, axis labels.

Each site answers at its own address (tastematch., deals., catcam.machaka.net) at "/", and under /tastematch, /deals
and /catcam of one address (lab.machaka.net, a development machine). Links in templates start with R, the site's
root ("" at its own address), so the same templates work both ways.
"""
import datetime as dt
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import charts, fmt
from .config import TZ

HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
fmt.register(templates.env)
templates.env.globals.update(charts=charts, LAB_URL="https://lab.machaka.net", SITE_LINKS=[
    ("tastematch", "Taste Match", "https://tastematch.machaka.net"), ("deals", "Deals", "https://deals.machaka.net"),
    ("catcam", "Cat cam", "https://catcam.machaka.net")])
SITE_OF = {"bgt": "tastematch", "deals": "deals", "cats": "catcam"}


def site(name):
    """A FastAPI app for one site, with the shared stylesheet and script under /static."""
    app = FastAPI(title=name, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    return app


def page(request, name, **ctx):
    me = SITE_OF.get(name.split("_")[0].split(".")[0], "")
    return templates.TemplateResponse(request, name, {"R": request.scope.get("root_path", "").rstrip("/"),
                                                      "me": me, "path": request.url.path, **ctx})


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
