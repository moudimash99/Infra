"""deals.machaka.net: the best board game deals on Okkazeo right now, what's new and cheap, bundles to judge."""
import datetime as dt
import os

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import charts, store
from .config import TZ
from .sources import bgf
from .web import day_labels, page, site

app = site("deals", [("/", "Deals", "tag"), ("/new", "New", "zap"), ("/bundles", "Bundles", "package"),
                    ("/games", "Games", "dices"), ("/status", "Status", "gauge")])


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    market = bgf.market.get()
    points = []
    for i, (day, n) in enumerate((market.data or {}).get("per_day", [])):
        d = dt.date.fromisoformat(day)
        points.append({"value": n or 0, "label": f"{d.day} {d:%b}" if i % 7 == 1 else "",
                       "tip": f"{d.day} {d:%b}: " + (f"{n:,} listings" if n else "no scrape")})
    return page(request, "deals.html", deals=bgf.deals.get(), market=market, run=bgf.run.get(),
                fresh=bgf.new_deals.get(), bundles=bgf.bundles.get(),
                per_day_points=points, history=store.job_history("boardgamefinder", days=30))


@app.get("/game/{bg_id}", response_class=HTMLResponse)
def game(request: Request, bg_id: int):
    r = bgf.game.get(bg_id)
    chart = None
    if r.ok and r.data and r.data["history"]:
        pts = [(dt.datetime.fromisoformat(h["day"]).replace(tzinfo=TZ).timestamp(), h["low"]) for h in r.data["history"]]
        chart = charts.lines([{"name": "Cheapest ask", "slot": 1, "points": pts}], title="Cheapest ask per day",
                             x_labels=day_labels([p[0] for p in pts]), unit=" €", step=True)
    return page(request, "deals_game.html", r=r, price_chart=chart)


FILES = {"digest": ("deals.html", "text/html"), "review-packet": ("review_packet.md", "text/plain; charset=utf-8"),
         "bundle-review": ("bundle_newsletter.html", "text/html")}


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
