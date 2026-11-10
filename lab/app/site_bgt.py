"""tastematch.machaka.net: which board game reviewers share the user's taste, the games and reviews behind it, and the
pipeline that turns YouTube reviews into verdicts.

  /                        the user's matches, games their matches love, the pipeline at a glance
  /reviewers               every host ranked by match; /reviewers/{id}: agreements, disagreements, every verdict
  /games                   every game with a verdict; /games/{bgg_id}: BGG's facts and every verdict
  /videos                  the round's reviews; /v/{id}: one review's verdicts and transcript
  /pipeline                how far the round is, what runs now, problems, runs
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
from .sources import bgt, taste
from .web import day_labels, page, site, templates

templates.env.globals.update(STAGES=bgt.STAGES)
# A host's avatar colour from their id (a better spread than the shared name sum): av-1 .. av-8.
templates.env.filters["tm_av"] = lambda key: f"av-{zlib.crc32(str(key).encode()) % 8 + 1}"
FAIL_LINE = re.compile(r"\[\d+/\d+\]\s+FAIL\s+([\w-]{6,20})\s+(.*?)\s+\((.*)\)\s*$")
app = site("tastematch", [("/", "Home", "home"), ("/reviewers", "Reviewers", "users"), ("/games", "Games", "dices"),
                          ("/videos", "Videos", "video"), ("/pipeline", "Pipeline", "activity")])
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
    recs = taste.recommendations(s, skip=(prof.data or {}).get("owned", ()), limit=8) if s else []
    return page(request, "tastematch/home.html", sc=sc, s=s, prof=prof, recs=recs, stats=taste.stats.get(),
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
                channels=channels, counts=counts, prof=taste.profile.get())


@app.get("/reviewers/{rid}", response_class=HTMLResponse)
def reviewer(request: Request, rid: str):
    if not re.fullmatch(r"[\w-]{1,20}:[\w.-]{1,60}", rid):
        raise HTTPException(404)
    sc, hv = taste.scores.get(), taste.host_verdicts.get(rid)
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
    return page(request, "tastematch/reviewer.html", active="/reviewers", sc=sc, s=s, r=r, rid=rid, hname=name,
                channel=chans.get(ch) or {"name": ch, "url": ""}, agree=agree, disagree=disagree, loves=loves,
                verdicts=verdicts, mean=(sum(scores) / len(scores)) if scores else None, hv=hv,
                things=taste.things.get().data or {}, show_all=request.query_params.get("all") == "1")


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
        rows.append({**e, "rated": rated, "say": taste.matches_say(s, e["gid"]) if s else None})
    keys = {"reviewed": lambda e: (-len(e["hosts"]), -e["avg"], e["name"].lower()),
            "matches": lambda e: (e["say"] is None, -(e["say"] or 0), -len(e["hosts"]), e["name"].lower()),
            "score": lambda e: (-round(e["avg"], 2), -len(e["hosts"]), e["name"].lower()),
            "name": lambda e: e["name"].lower()}
    sort = sort if sort in keys else "reviewed"
    rows.sort(key=keys[sort])
    pg = _pager(_page_no(request), len(rows), GAME_PAGE)
    shown = rows[(pg["page"] - 1) * GAME_PAGE: pg["page"] * GAME_PAGE]
    n_rated = sum(1 for v in rated_of.values() if v is not None)
    counts = {"all": len(every), "rated": n_rated, "unrated": len(every) - n_rated}
    return page(request, "tastematch/games.html", g=g, rows=shown, pg=pg, q=q, show=show, sort=sort, counts=counts,
                s=s)


@app.get("/games/{gid}", response_class=HTMLResponse)
def game(request: Request, gid: int):
    canon = taste.canonical.get().data or {}
    if canon.get(gid, gid) != gid:          # an edition: its game's page
        return RedirectResponse(f"{request.scope.get('root_path', '').rstrip('/')}/games/{canon[gid]}", 301)
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
    scores = [v["score"] for v in verdicts if v["score"] is not None]
    return page(request, "tastematch/game.html", active="/games", r=r, g=r.data, verdicts=verdicts, mine=mine,
                say=taste.matches_say(s, gid) if s else None, avg=(sum(scores) / len(scores)) if scores else None,
                chans=bgt.channel_info.get().data or {})


@app.get("/img/{gid}-{size}.jpg")
async def box_art(gid: int, size: str):
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
