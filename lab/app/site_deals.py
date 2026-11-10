"""deals.machaka.net: board game bargains on Okkazeo, from BoardGameFinder's morning run.

  /            the best deals right now, and what's new, cheaper or waiting for a look
  /new         listings that appeared in the last day, 3 days or week: under their usual price first
  /drops       listings whose price went down
  /bundles     lots, special editions and played legacy copies, which no rule can value: to judge by eye
  /games       every game BoardGameFinder follows, with what's for sale; /game/{id} one game, its price history
  /status      how the runs went, step by step
"""
import datetime as dt
import os
import time
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import charts, images, store
from .config import TZ
from .sources import bgf
from .web import day_labels, page, site, templates

app = site("deals", [("/", "Deals", "tag"), ("/new", "New", "zap"), ("/bundles", "Bundles", "package"),
                    ("/games", "Games", "dices"), ("/status", "Status", "gauge")])
templates.env.globals.update(deal_cond=bgf.cond, deal_edition=bgf.edition, deal_age=bgf.age)
PERIODS = {1: "24 hours", 3: "3 days", 7: "7 days"}
PER_PAGE = 48


def _health(market):
    """'failed' when the last check found no live listing at all, 'stale' when it is over 30 hours old."""
    m = market.data
    if not m:
        return None
    if m["live"] == 0:
        return "failed"
    if m["last_scrape"] and time.time() - m["last_scrape"] > 30 * 3600:
        return "stale"
    return None


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    market = bgf.market.get()
    return page(request, "deals/home.html", market=market, best=bgf.deals.get(), fresh=bgf.new_listings.get(1),
                bundles=bgf.bundles.get(), run=bgf.run.get(), health=_health(market))


@app.get("/new", response_class=HTMLResponse)
def new(request: Request, days: int = 1):
    days = days if days in PERIODS else 1
    return page(request, "deals/new.html", days=days, periods=PERIODS, r=bgf.new_listings.get(days))


@app.get("/drops", response_class=HTMLResponse)
def drops(request: Request, days: int = 3):
    days = days if days in PERIODS else 3
    return page(request, "deals/drops.html", days=days, periods=PERIODS, r=bgf.drops.get(days), active="/")


@app.get("/bundles", response_class=HTMLResponse)
def bundles(request: Request, kind: str = ""):
    return page(request, "deals/bundles.html", r=bgf.bundles.get(), kind=kind)


SORTS = {"listings": "Most for sale", "price": "Best price", "name": "A to Z"}


@app.get("/games", response_class=HTMLResponse)
def games(request: Request, q: str = "", sort: str = "listings", show: str = "", p: int = 1):
    sort = sort if sort in SORTS else "listings"
    r = bgf.games.get()
    every = r.data or []
    found = list(every)
    q = q.strip()[:60]
    if q:
        words = q.lower().split()
        found = [g for g in found if all(w in (g["name"] + " " + g["fr"]).lower() for w in words)]
    elif show != "all":
        found = [g for g in found if g["live"]]
    if sort == "name":
        found.sort(key=lambda g: g["name"].lower())
    elif sort == "price":       # the cheapest copy furthest under the typical ask first (needs 3 copies to say)
        found.sort(key=lambda g: (g["under"] is None, -(g["under"] or 0), g["name"].lower()))
    else:
        found.sort(key=lambda g: (-g["live"], g["name"].lower()))
    pages = max(1, -(-len(found) // PER_PAGE))
    p = min(max(1, p), pages)

    def qs(**change):
        """This page's query with some values changed: '?q=ark&sort=price'; defaults left out."""
        now = {"q": q, "sort": sort, "show": show, "p": 1, **change}
        return "?" + urlencode({k: v for k, v in now.items()
                                if v and not (k == "p" and v == 1) and not (k == "sort" and v == "listings")})
    return page(request, "deals/games.html", r=r, q=q, sort=sort, sorts=SORTS, show=show, found=found, qs=qs,
                shown=found[(p - 1) * PER_PAGE:p * PER_PAGE], page_no=p, pages=pages, n_all=len(every),
                n_live=sum(1 for g in every if g["live"]))


@app.get("/game/{bg_id}", response_class=HTMLResponse)
def game(request: Request, bg_id: int):
    r = bgf.game.get(bg_id)
    if r.ok and r.data is None:
        raise HTTPException(404, "BoardGameFinder doesn't follow this game")
    chart = None
    if r.ok and r.data and len(r.data["history"]) > 1:
        pts = [(dt.datetime.fromisoformat(h["day"]).replace(tzinfo=TZ).timestamp(), h["low"]) for h in r.data["history"]]
        chart = charts.lines([{"name": "Cheapest copy", "slot": 1, "points": pts}],
                             title="Cheapest copy for sale, per check", x_labels=day_labels([p[0] for p in pts]),
                             unit=" €", step=True)
    return page(request, "deals/game.html", r=r, price_chart=chart, active="/games")


FILES = {"digest": ("deals.html", "text/html"), "review-packet": ("review_packet.md", "text/plain; charset=utf-8"),
         "bundle-review": ("bundle_newsletter.html", "text/html")}


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    market = bgf.market.get()
    points = []
    for i, (day, n) in enumerate((market.data or {}).get("per_day", [])):
        d = dt.date.fromisoformat(day)
        points.append({"value": n or 0, "label": f"{d.day} {d:%b}" if i % 7 == 1 else "",
                       "tip": f"{d.day} {d:%b}: " + (f"{n:,} listings" if n else "no check")})
    files = []
    for name, label, sub in [("digest", "The emailed digest", "The deals mail, as sent"),
                             ("bundle-review", "The last LLM bundle review", "What the LLM said about bundles"),
                             ("review-packet", "The bundle packet", "What the LLM is asked to judge")]:
        path = os.path.join(bgf.BGF_DIR, "Output", FILES[name][0])
        if os.path.exists(path):
            files.append({"name": name, "label": label, "sub": sub, "at": os.path.getmtime(path)})
    return page(request, "deals/status.html", run=bgf.run.get(), market=market, per_day_points=points,
                history=store.job_history("boardgamefinder", days=30), files=files, health=_health(market))


@app.get("/img/{bg_id}-{size}.jpg")
async def picture(bg_id: int, size: str):
    """A game's box art (BGG's picture, by its BGG id), shrunk once and kept by the lab."""
    return await images.serve("bgg", bg_id, size, lambda: bgf.image_url(bg_id))


@app.get("/file/{name}")
def output_file(name: str):
    """BoardGameFinder's own outputs, by name from this list only."""
    if name not in FILES:
        raise HTTPException(404)
    fname, media = FILES[name]
    path = os.path.join(bgf.BGF_DIR, "Output", fname)
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type=media)
