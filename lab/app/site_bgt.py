"""tastematch.machaka.net: which board game reviewers share the user's taste, the games and reviews behind it, and the
pipeline that turns YouTube reviews into verdicts.

  /                        the user's matches, games their matches love, the pipeline at a glance
  /reviewers               every host ranked by match; /reviewers/{id}: agreements, disagreements, every verdict
  /games                   every game with a verdict; /games/{bgg_id}: BGG's facts and every verdict
  /videos                  the round's reviews; /v/{id}: one review's verdicts and transcript
  /pipeline                how far the round is, what runs now, problems, runs
  /find, /u/{name}         anyone's matches from their BGG username (sources/visitors.py); ?u={name} on a reviewer
                           page shows it against that visitor
  /about                   how it works: where the verdicts come from, how a match is worked out
  /img/{bgg_id}-{s|m|l}.jpg  box art, fetched and shrunk by the lab (images.py)
"""
import math
import os
import re
import zlib
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import charts, images, store
from .sources import Reading, bgt, insights, taste, visitors
from .access import OPEN_HOSTS
from .web import day_labels, page, site, templates

templates.env.globals.update(STAGES=bgt.STAGES)
# A host's avatar colour from their id (a better spread than the shared name sum): av-1 .. av-8.
templates.env.filters["tm_av"] = lambda key: f"av-{zlib.crc32(str(key).encode()) % 8 + 1}"
FAIL_LINE = re.compile(r"\[\d+/\d+\]\s+FAIL\s+([\w-]{6,20})\s+(.*?)\s+\((.*)\)\s*$")
app = site("tastematch", [("/", "Home", "home"), ("/for-you", "For you", "sparkle"), ("/discover", "Discover", "trending-up"),
                          ("/reviewers", "Reviewers", "users"), ("/games", "Games", "dices"), ("/find", "Find yours", "search"),
                          ("/pipeline", "Pipeline", "activity")])
GAME_PAGE = 24


def _page_no(request):
    p = request.query_params.get("page", "1")
    return max(1, int(p)) if p.isdigit() else 1


def _pager(page_no, total, per):
    pages = max(1, math.ceil(total / per))
    return {"page": min(page_no, pages), "pages": pages, "total": total}


# ---- home ---------------------------------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    sc, prof = taste.scores.get(), taste.profile.get()
    s = sc.data
    return page(request, "tastematch/home.html", sc=sc, s=s, prof=prof, stats=taste.stats.get(),
                taste_prof=insights.taste_profile(s) if s else None,
                things=taste.things.get().data or {}, recent=taste.recent.get(), prog=bgt.progress.get(),
                pipe=bgt.now.get())


# ---- reviewers ------------------------------------------------------------------------------------------------------

@app.get("/reviewers", response_class=HTMLResponse)
def reviewers(request: Request, show: str = "ranked", channel: str = ""):
    sc = taste.scores.get()
    s = sc.data
    show = show if show in ("ranked", "thin", "all") else "ranked"
    everyone = s["reviewers"] if s else []
    rows = [r for r in everyone if (show == "all" or (show == "ranked") == r["ranked"])
            and (not channel or r["channel"] == channel)]
    channels = sorted({(r["channel"], r["channel_name"]) for r in everyone}, key=lambda c: c[1])
    counts = {"ranked": sum(r["ranked"] for r in everyone), "thin": sum(not r["ranked"] for r in everyone),
              "all": len(everyone)}
    return page(request, "tastematch/reviewers.html", sc=sc, s=s, rows=rows, show=show, channel=channel,
                channels=channels, counts=counts)


def _root(request):
    return request.scope.get("root_path", "").rstrip("/")


def _is_open(request):
    """Is this request for a name without a login (access.OPEN_HOSTS)? Pages there never show the owner's data."""
    return (request.headers.get("host") or "").split(":")[0].lower().rstrip(".") in OPEN_HOSTS


def _asked(request):
    """The visitor name a page was asked for (the last ?u=, as Starlette reads it), or None without ?u=."""
    if "u" not in request.query_params:
        return None
    return " ".join(request.query_params.get("u", "").split())


def _visitor(request):
    """(name, their result) when ?u= names a visitor whose match is ready, else (None, None)."""
    name = _asked(request)
    if not name or not visitors.valid(name):
        return None, None
    res = visitors.result(name)
    return (res["username"], res) if res else (None, None)


def _for_whom(request):
    """The match a page is about -> (Reading of it, visitor name or None, None), or (None, None, response) when the
    page must answer otherwise: ?u= naming someone whose match isn't ready -> their page; a bad ?u= -> 404; on an
    open name without a visitor -> 404, so the owner's match is never shown there."""
    asked = _asked(request)
    vname, vres = _visitor(request)
    if vres:
        return Reading(True, vres, "", vres["at"]), vname, None
    if asked is not None:
        if not visitors.valid(asked):
            raise HTTPException(404)
        return None, None, RedirectResponse(f"{_root(request)}/u/{quote(asked)}", status_code=303)
    if _is_open(request):
        raise HTTPException(404)
    return taste.scores.get(), None, None


@app.get("/reviewers/{rid}", response_class=HTMLResponse)
def reviewer(request: Request, rid: str):
    if not re.fullmatch(r"[\w-]{1,20}:[\w.-]{1,60}", rid):
        raise HTTPException(404)
    sc, vname, answer = _for_whom(request)       # a visitor's page never falls back to the owner's match
    if answer:
        return answer
    hv = taste.host_verdicts.get(rid)
    s = sc.data
    r = s["by_id"].get(rid) if s else None
    if not r and not (hv.data and hv.data["host"]):
        raise HTTPException(404)
    agree, disagree, loves = taste.shared_lists(s, r) if r else ([], [], [])
    chans = bgt.channel_info.get().data or {}
    host = hv.data["host"] if hv.data else None
    ch = (r or {}).get("channel") or (host or {}).get("channel", "")
    name = r["name"] if r else taste.nice_name((host or {}).get("name", rid))
    verdicts = (hv.data or {}).get("verdicts", [])
    scores = [v["score"] for v in verdicts if v["score"] is not None]
    things = taste.things.get().data or {}
    sort = request.query_params.get("sort", "agree")
    sort = sort if sort in taste.COMMON_SORTS else "agree"
    return page(request, "tastematch/reviewer.html", active="/reviewers", sc=sc, s=s, r=r, rid=rid, hname=name,
                channel=chans.get(ch) or {"name": ch, "url": ""}, agree=agree, disagree=disagree, loves=loves,
                verdicts=verdicts, mean=(sum(scores) / len(scores)) if scores else None, hv=hv,
                things=things, show_all=request.query_params.get("all") == "1",
                common=taste.common_games(s, r, sort, things) if r else [], sort=sort, sorts=taste.COMMON_SORTS,
                dist_them=taste.distribution(scores),
                dist_you=taste.distribution([v[0] / 2 for v in s["user"].values()]) if s else [],
                you_mean=(sum(v[0] for v in s["user"].values()) / 2 / len(s["user"])) if s and s["user"] else None,
                visitor=vname, UQ=("?u=" + quote(vname)) if vname else "", groups=taste.by_game(verdicts),
                host_prof=insights.host_profile(r) if r else None)


# ---- anyone's matches -----------------------------------------------------------------------------------------------

def _client(request):
    """Who is asking (their IP: Cloudflare's header first), for the per-client limit."""
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return request.headers.get("cf-connecting-ip") or fwd or (request.client.host if request.client else "")


@app.get("/find", response_class=HTMLResponse)
def find(request: Request, name: str = ""):
    """The only page that starts a match (a visit to /u/{name} never does, so a crawler can't fill the queue)."""
    name = " ".join(name.split())
    if name:
        try:
            visitors.ask(name, client=_client(request))
            return RedirectResponse(f"{_root(request)}/u/{quote(name)}", status_code=303)
        except visitors.VisitorError as e:
            return page(request, "tastematch/find.html", uname=name, error=str(e), stats=taste.stats.get())
    return page(request, "tastematch/find.html", uname="", error="", stats=taste.stats.get())


@app.get("/u/{name}", response_class=HTMLResponse)
def visitor(request: Request, name: str):
    name = " ".join(name.split())
    if not visitors.valid(name):
        raise HTTPException(404)
    st = visitors.status(name)
    s = visitors.result(name) if st and st["state"] == "done" else None
    if st and st["state"] == "done" and not s:      # over a day old: forgotten; a new match starts from /find
        st = None
    if not s:
        return page(request, "tastematch/visitor_wait.html", active="/find", uname=name, st=st)
    things = taste.things.get().data or {}
    return page(request, "tastematch/visitor.html", active="/find", uname=s["username"], s=s, st=st, things=things,
                UQ="?u=" + quote(s["username"]),
                taste_prof=insights.taste_profile(s),
                with_reviews=len(set(s["user"]) & insights.reviewed_ids(s)),
                thin=[r for r in s["reviewers"] if 0 < r["n"] and not r["ranked"]])


@app.get("/u/{name}/status")
def visitor_status(name: str):
    """For the wait page's quiet polling: {state, step, ahead, error}."""
    name = " ".join(name.split())
    if not visitors.valid(name):
        raise HTTPException(404)
    st = visitors.status(name) or {}
    return {k: st.get(k) for k in ("state", "step", "ahead", "error")}


@app.get("/for-you", response_class=HTMLResponse)
def for_you(request: Request, players: int = 0, weight: str = "", time: int = 0):
    sc, vname, answer = _for_whom(request)
    if answer:
        return answer
    s = sc.data
    players = players if 1 <= players <= 12 else 0
    weight = weight if weight in insights.WEIGHT_BANDS else ""
    time = time if time in (30, 60, 90, 120, 180) else 0
    picks = insights.for_you(s, players or None, weight, time or None, limit=36) if s else []
    return page(request, "tastematch/for_you.html", active="/for-you", s=s, picks=picks, players=players,
                weight=weight, time=time, visitor=vname, UQ=("?u=" + quote(vname)) if vname else "",
                taste_prof=insights.taste_profile(s) if s else None)


@app.get("/discover", response_class=HTMLResponse)
def discover(request: Request, rated: str = ""):
    """Made from the hosts' verdicts only. For the owner (never on an open name) it leaves out the games they rated,
    since it is for finding games; ?rated=1 shows them too."""
    s = taste.scores.get().data
    d = insights.discover(s) if s else None
    show_rated = rated == "1" or _is_open(request)
    hidden = 0
    if d and not show_rated:
        mine = set(s["user"])
        for key in ("gems", "overrated", "divisive"):
            hidden += sum(1 for r in d[key] if r["gid"] in mine)
            d[key] = [r for r in d[key] if r["gid"] not in mine]
    return page(request, "tastematch/discover.html", active="/discover", d=d, show_rated=show_rated,
                hidden=hidden, can_hide=not _is_open(request))


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return page(request, "tastematch/about.html", active="/find", stats=taste.stats.get())


# ---- games ----------------------------------------------------------------------------------------------------------

@app.get("/games", response_class=HTMLResponse)
def games(request: Request, q: str = "", show: str = "all", sort: str = "reviewed"):
    q = q.strip()[:80]
    g = taste.games.get()
    sc, prof = taste.scores.get(), taste.profile.get()
    s = sc.data
    mine = (prof.data or {}).get("games", {})
    every = list((g.data or {}).values())
    rated_of = {e["gid"]: (mine.get(e["gid"]) or {}).get("rating") for e in every}
    rows = []
    for e in every:
        rated = rated_of[e["gid"]]
        if (show == "rated" and rated is None) or (show == "unrated" and rated is not None):
            continue
        if q and q.lower() not in e["name"].lower():
            continue
        rows.append({**e, "rated": rated})
    keys = {"reviewed": lambda e: (-len(e["hosts"]), -e["avg"], e["name"].lower()),
            "score": lambda e: (-round(e["avg"], 2), -len(e["hosts"]), e["name"].lower()),
            "name": lambda e: e["name"].lower()}
    sort = sort if sort in keys else "reviewed"
    rows.sort(key=keys[sort])
    pg = _pager(_page_no(request), len(rows), GAME_PAGE)
    shown = rows[(pg["page"] - 1) * GAME_PAGE: pg["page"] * GAME_PAGE]
    n_rated = sum(1 for v in rated_of.values() if v is not None)
    counts = {"all": len(every), "rated": n_rated, "unrated": len(every) - n_rated}
    return page(request, "tastematch/games.html", g=g, rows=shown, pg=pg, q=q, show=show, sort=sort, counts=counts,)


@app.get("/games/{gid}", response_class=HTMLResponse)
def game(request: Request, gid: int):
    canon = taste.canonical.get().data or {}
    if canon.get(gid, gid) != gid:          # an edition: its game's page
        return RedirectResponse(f"{_root(request)}/games/{canon[gid]}", 302)     # editions are regrouped: not 301
    r = taste.game.get(gid)
    if r.ok and r.data is None:
        raise HTTPException(404)
    sc, prof = taste.scores.get(), taste.profile.get()
    s = sc.data
    mine = ((prof.data or {}).get("games", {}).get(gid)) or {}
    verdicts = [{**v, "m": (s["by_id"].get(v["rid"]) if s else None)} for v in (r.data or {}).get("verdicts", [])]
    # Hosts who match the user first, closest first; then everyone else by score.
    verdicts.sort(key=lambda v: (not (v["m"] and v["m"]["ranked"]),
                                 -(v["m"]["match"] if v["m"] and v["m"]["ranked"] else 0), -(v["score"] or 0)))
    scores = [v["score"] for v in verdicts if v["score"] is not None and not v.get("not_a_verdict")
              and not (v.get("host") or "").lower().startswith(("unknown", "team"))]
    hosts = taste.by_host(verdicts)
    hosts.sort(key=lambda h: h["unnamed"])                 # keeps the match order; unnamed speakers last
    return page(request, "tastematch/game.html", active="/games", r=r, g=r.data, verdicts=verdicts, mine=mine, hosts=hosts,
                likely=insights.likely(s, gid) if s and not mine.get("rating") else None,
                avg=(sum(scores) / len(scores)) if scores else None, n_scores=len(scores),
                chans=bgt.channel_info.get().data or {})


@app.get("/img/{gid}-{size}.jpg")
async def box_art(request: Request, gid: int, size: str):
    if _is_open(request) and gid not in (taste.games.get().data or {}):   # open names: reviewed games only, so
        raise HTTPException(404)                                         # the images say nothing of a collection
    return await images.serve("bgg", gid, size, lambda: taste.image_url(gid))


# ---- videos ---------------------------------------------------------------------------------------------------------

@app.get("/videos", response_class=HTMLResponse)
def videos(request: Request, show: str = "rated", channel: str = "", q: str = ""):
    show = show if show in ("rated", "waiting", "all") else "rated"
    q = q.strip()[:80]
    chans = bgt.channel_info.get().data or {}
    channel = channel if channel in chans else ""
    page_no = _page_no(request)
    v = taste.videos.get(show, channel, q, page_no)
    pg = _pager(page_no, (v.data or {}).get("total", 0), taste.VIDEO_PAGE)
    return page(request, "tastematch/videos.html", v=v, pg=pg, show=show, channel=channel, q=q, chans=chans,
                files=bgt.stage_files.get().data or {})


@app.get("/search")
def search(request: Request, q: str = ""):
    """The old search address: now the videos page, the whole round."""
    root = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(f"{root}/videos?show=all&q={quote(q[:80])}", 301)


@app.get("/v/{vid}", response_class=HTMLResponse)
def video(request: Request, vid: str):
    if not re.fullmatch(r"[\w-]{6,20}", vid):
        raise HTTPException(404)
    r = bgt.video.get(vid)
    if r.ok and r.data is None:
        raise HTTPException(404)
    s = taste.scores.get().data
    ratings = [{**x, "m": (s["by_id"].get(x.get("reviewer_id") or "") if s else None)}
               for x in (r.data or {}).get("ratings", [])]
    return page(request, "tastematch/video.html", active="/videos", r=r, ratings=ratings,
                chans=bgt.channel_info.get().data or {}, canon=taste.canonical.get().data or {})


# ---- pipeline -------------------------------------------------------------------------------------------------------

@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request):
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
        for st in run["summary"].get("stages", []):
            audio += st.get("audio_min", 0)
            took += st.get("took_min", 0)
            for f in st.get("fails", []):
                m = FAIL_LINE.search(f)       # "[41/114] FAIL <id> <title> (ERROR: ...)" -> the video and why
                why = (m.group(3).removeprefix("ERROR:").strip() or "no reason given") if m else ""
                fails.append({"line": f, "run": run["name"], "at": run.get("start"), "vid": m.group(1) if m else None,
                              "title": m.group(2) if m else f, "why": why.replace("unable to download video data: ", "")})
    return page(request, "tastematch/pipeline.html", prog=prog, pipe=now, activity=bgt.activity.get(),
                sorting=bgt.sorting.get(), eta=bgt.eta,
                problems=bgt.problems.get(), progress_chart=chart, has_results=bool(bgt.results_file()), fails=fails,
                whisper_speed=(audio / took) if took > 30 else None,
                runs=store.job_history(prefix="bg-taste-match", days=7))


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
