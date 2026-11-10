"""BoardGameFinder: the best deals now, new listings, price drops, bundles to judge, every followed game, the runs.

Reads /srv/apps/boardgamefinder/data read-only: Games.db (opened mode=ro), Output/deals.csv (the morning's best
deals), Output/scored.csv (every live listing under its usual price; deal_finder writes it since 2026-11-10),
Output/review_pending.json (bundles to judge), Output/log.txt and pipeline_state.json. Never the repo itself (it
holds credentials).

The words on the pages are BoardGameFinder's (valuation.py):
  usual price  Baseline: what the game sells for, from sold copies and asking prices, adjusted for condition
  saving       Net: the usual price minus the ask minus the costs (one Mondial Relay leg, 7.99 €, unless it can be
               collected near Toulouse)
"""
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import statistics

import httpx

from . import source, k8s
from .. import logs, store
from ..config import BGF_DIR, BGT_DIR, TZ

STEPS = [  # run_pipeline.py order; critical steps stop the run
    ("top_500_games", "BGG top 500", False), ("update_all_ranks", "BGG ranks", False),
    ("bgg_hotness", "BGG hotness", False), ("fetch_legacy_flags", "Legacy flags", False),
    ("Okkazeo_games_finder", "Find game pages", False), ("price_finder", "Scrape listings", True),
    ("fetch_descriptions", "Seller descriptions", False), ("classify_postings_with_llm", "Classify listings (LLM)", False),
    ("deal_finder", "Find deals + email", True), ("build_review_packet", "Bundle packet", False),
    ("llm_review", "Bundle review (LLM)", False),
]
LLM_STEPS = ("classify_postings_with_llm", "llm_review")     # wait for fm-llm on the server
MIN_SAVING = 1.0    # under the usual price by at least this, after shipping, to be listed as cheaper (no "save 0 €")

CONDITIONS = {"neuf_cello": "New, sealed", "juste_deballe": "Opened, unplayed", "etat_excellent": "Excellent",
              "tres_bon_etat": "Very good", "bon_etat": "Good", "etat_correct": "Fair", "etat_moyen": "Fair",
              "abime": "Damaged", "mauvais_etat": "Poor"}
EDITIONS = {"VF": "French", "VO": "Original language"}


def cond(code):
    """Okkazeo's condition code in plain English ('etat_excellent' -> 'Excellent')."""
    return CONDITIONS.get(code or "", (code or "").replace("_", " ").capitalize())


def edition(code):
    return EDITIONS.get(code or "", code or "")


def db():
    conn = sqlite3.connect(f"file:{os.path.join(BGF_DIR, 'Games.db')}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _images(conn):
    img = {r[0]: r[1] for r in conn.execute("SELECT BG_Id, Image_URL FROM Game_main_page_link WHERE Image_URL != ''")}
    img.update({r[0]: r[1] for r in conn.execute("SELECT ID, Image_URL FROM BGG_Games WHERE Image_URL != ''")})
    return img


@source("bgf.images", ttl=900)
def image_map():
    conn = db()
    try:
        return _images(conn)
    finally:
        conn.close()


_bgg = httpx.Client(timeout=10, headers={"User-Agent": "machaka-lab/1.0 (+https://lab.machaka.net)"})
_bgg_urls = {}      # BGG id -> the address of BGG's picture, once found


def _bgg_picture(bg_id):
    """BGG's box art for a BGG id (BoardGameFinder's BG_ID is one): from bg-taste-match's copy of BGG's data when it
    has the game (no call), else from BGG's item API, once per game (the lab keeps the shrunk picture)."""
    if bg_id in _bgg_urls:
        return _bgg_urls[bg_id]
    url = None
    try:
        conn = sqlite3.connect(f"file:{os.path.join(BGT_DIR, 'bgtaste.db')}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT json_extract(record, '$.image') FROM bgg_things WHERE id = ?", (bg_id,)).fetchone()
        finally:
            conn.close()
        url = row[0] if row else None
    except sqlite3.Error:
        pass
    if not url:
        try:
            r = _bgg.get("https://api.geekdo.com/api/geekitems",
                         params={"objectid": bg_id, "objecttype": "thing", "nosession": 1})
            r.raise_for_status()
            item = r.json().get("item") or {}
            url = (item.get("images") or {}).get("original") or item.get("imageurl")
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    if url:
        _bgg_urls[bg_id] = url
    return url


def image_url(bg_id):
    """Where a game's box art is, for images.serve: BGG's picture; else Okkazeo's, but only an address
    BoardGameFinder read off the game's page (a file name like 65e8782c41eab3.69816426.jpg). The ones it guessed
    from the page's number (/images/jeux/5576.jpg) are often another game's box (5576 is Skyline, not Dixit)."""
    url = _bgg_picture(bg_id)
    if url:
        return url
    okz = (image_map.get().data or {}).get(bg_id) or ""
    return okz if okz and not re.search(r"/images/jeux/\d+\.\w+$", okz) else None


def _paris(s):
    """BoardGameFinder writes naive local (Paris) times: '2026-11-10 07:43:51.02' -> epoch."""
    try:
        return dt.datetime.fromisoformat(str(s)[:19]).replace(tzinfo=TZ).timestamp()
    except (TypeError, ValueError):
        return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    return round(100 * part / whole) if part and whole else None


def _place(location):
    """'Paris (75013)\\n08/11/2026' -> 'Paris (75013)'."""
    return (location or "").split("\n")[0].strip()


def _since(days):
    return (dt.datetime.now(TZ) - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# A live listing with its game's name, BGG rank and year; _listing() turns a row into what the pages show.
POST = """SELECT p.Link, p.Price, p.Condition, p.Ad_Language, p.Location, p.Can_Ship, p.First_Seen, p.Post_Date,
                 p.Is_Bundle, p.Is_Special_Edition, p.Is_Extension, p.BG_ID, p.Listing_Text, p.Seller_Description,
                 COALESCE(g.Name, l.Name) AS name, g.Rank, g.Year
          FROM Post p LEFT JOIN BGG_Games g ON g.ID = p.BG_ID
          LEFT JOIN (SELECT BG_Id, MIN(Name) AS Name FROM Game_main_page_link GROUP BY BG_Id) l ON l.BG_Id = p.BG_ID"""


def _listing(r, valued):
    v = valued.get(r["Link"]) or {}
    out = {"link": r["Link"], "price": r["Price"], "condition": r["Condition"] or "", "language": r["Ad_Language"] or "",
           "place": _place(r["Location"]), "ships": bool(r["Can_Ship"]), "first_seen": _paris(r["First_Seen"]),
           "posted": _paris(r["Post_Date"]), "bundle": bool(r["Is_Bundle"]), "special": bool(r["Is_Special_Edition"]),
           "extension": bool(r["Is_Extension"]), "bg_id": r["BG_ID"], "name": r["name"] or "Unknown game",
           "rank": r["Rank"], "year": r["Year"], "image": bool(r["BG_ID"]),
           "baseline": v.get("baseline"), "net": v.get("net")}
    out["saving_pct"] = _pct(out["net"], out["baseline"])
    # sellers can bump an ad's date, so an ad is as old as the earliest date known (as deal_finder counts it)
    out["listed"] = min(filter(None, (out["posted"], out["first_seen"])), default=None)
    return out


def age(ts, now=None):
    """How long ago a listing went up, in days: 'today', 'yesterday', '5 days ago', '28 Aug', '21 Dec 2025'."""
    if not ts:
        return ""
    today = dt.datetime.fromtimestamp(now or dt.datetime.now().timestamp(), TZ).date()
    day = dt.datetime.fromtimestamp(ts, TZ).date()
    days = (today - day).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    return f"{day.day} {day:%b}" + (f" {day.year}" if day.year != today.year else "")


def _read_scored():
    """Output/scored.csv: every live listing BoardGameFinder valued under its usual price (deal_finder writes it
    since 2026-11-10), with that usual price (Baseline), how far under it the ask is (Edge) and the saving after
    shipping (Net)."""
    path = os.path.join(BGF_DIR, "Output", "scored.csv")
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f)), os.path.getmtime(path)


@source("bgf.scored", ttl=300)
def scored():
    rows, at = _read_scored()
    if rows is None:
        return {"available": False, "by_link": {}, "usual": {}, "file_time": None}
    by_link, usual = {}, {}
    for r in rows:
        base, bg = _f(r.get("Baseline")), int(_f(r.get("BG_ID")) or 0)
        by_link[r["Link"]] = {"baseline": base, "net": _f(r.get("Net")), "edge": _f(r.get("Edge"))}
        if base:
            usual[bg] = base
    return {"available": True, "by_link": by_link, "usual": usual, "file_time": at}


@source("bgf.deals", ttl=300)
def deals():
    """The morning's best deals (deals.csv): biggest saving after shipping first, games the user doesn't own."""
    path = os.path.join(BGF_DIR, "Output", "deals.csv")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    conn = db()
    try:
        marks = ",".join("?" * len(rows)) or "''"
        posts = {r["Link"]: r for r in conn.execute(
            f"SELECT Link, Alerted_At, First_Seen, Location, Is_Online FROM Post WHERE Link IN ({marks})",
            [r["Link"] for r in rows])}
        ranks = {r[0]: r[1] for r in conn.execute("SELECT ID, Rank FROM BGG_Games")}
    finally:
        conn.close()
    out = []
    for r in rows:
        bg = int(_f(r.get("BG_ID")) or 0)
        p = posts.get(r["Link"])
        d = {"name": r["Name"], "price": _f(r["Price"]), "baseline": _f(r["Baseline"]), "edge": _f(r["Edge"]),
             "net": _f(r["Net"]), "comps": int(_f(r.get("Sold_Comps")) or 0), "condition": r.get("Condition", ""),
             "language": r.get("Language", ""), "bundle": r.get("Bundle", ""), "ships": r.get("Ships") == "yes",
             "link": r["Link"], "posted": _paris(r.get("Post_Date")), "bg_id": bg, "image": bool(bg),
             "rank": ranks.get(bg), "place": _place(p["Location"]) if p else "",
             "online": bool(p["Is_Online"]) if p else True,
             "alerted": _paris(p["Alerted_At"]) if p else None, "first_seen": _paris(p["First_Seen"]) if p else None}
        d["saving_pct"] = _pct(d["net"], d["baseline"])
        out.append(d)
    return {"deals": out, "file_time": os.path.getmtime(path) if os.path.exists(path) else None}


@source("bgf.new", ttl=300)
def new_listings(days=1):
    """Live listings first seen in the last `days`: those under their usual price (scored.csv), biggest saving
    first, and the rest, newest first."""
    valued = scored.get().data or {"available": False, "by_link": {}, "file_time": None}
    conn = db()
    try:
        rows = conn.execute(f"""{POST} WHERE p.Is_Online = 1 AND COALESCE(p.Ignore, 0) = 0
                                AND p.First_Seen LIKE '20%' AND p.First_Seen >= ? ORDER BY p.First_Seen DESC""",
                            (_since(days),)).fetchall()
    finally:
        conn.close()
    items = [_listing(r, valued["by_link"]) for r in rows]
    cheap = sorted((i for i in items if (i["net"] or 0) >= MIN_SAVING), key=lambda i: -i["net"])
    rest = [i for i in items if not (i["net"] or 0) >= MIN_SAVING]
    return {"days": days, "available": valued["available"], "cheap": cheap, "rest": rest, "total": len(items),
            "file_time": valued["file_time"]}


@source("bgf.new_deals", ttl=300)
def new_deals():
    """Listings first seen in the last 24 hours that are under their usual price, biggest saving first."""
    n = new_listings.get(1).data
    return {"available": n["available"], "deals": n["cheap"], "file_time": n["file_time"]}


@source("bgf.drops", ttl=600)
def drops(days=3):
    """Live listings cheaper at their latest check than at the check before, the latest within `days`, biggest
    drop first. A day can hold two runs, so prices are taken per day."""
    conn = db()
    try:
        today = dt.datetime.now(TZ).date()
        rows = conn.execute("""
            WITH d AS (SELECT Link, substr(Date, 1, 10) AS day, MIN(Price) AS price FROM Price_History
                       WHERE Date >= ? GROUP BY Link, day),
                 r AS (SELECT Link, day, price, ROW_NUMBER() OVER (PARTITION BY Link ORDER BY day DESC) AS k FROM d)
            SELECT a.Link, a.price, b.price AS before, a.day, b.day AS before_day, p.BG_ID, p.Condition, p.Location,
                   p.Can_Ship, p.Ad_Language, COALESCE(g.Name, l.Name, '') AS name, g.Rank
            FROM r a JOIN r b ON b.Link = a.Link AND a.k = 1 AND b.k = 2
            JOIN Post p ON p.Link = a.Link
            LEFT JOIN BGG_Games g ON g.ID = p.BG_ID
            LEFT JOIN (SELECT BG_Id, MIN(Name) AS Name FROM Game_main_page_link GROUP BY BG_Id) l ON l.BG_Id = p.BG_ID
            WHERE a.price < b.price AND a.day >= ? AND p.Is_Online = 1
            ORDER BY (b.price - a.price) / b.price DESC""",
                            (str(today - dt.timedelta(days=60)), str(today - dt.timedelta(days=days)))).fetchall()
    finally:
        conn.close()
    return [{"link": r["Link"], "price": r["price"], "before": r["before"], "day": r["day"],
             "before_day": r["before_day"], "bg_id": r["BG_ID"], "condition": r["Condition"] or "",
             "place": _place(r["Location"]), "ships": bool(r["Can_Ship"]), "language": r["Ad_Language"] or "",
             "name": r["name"] or "Unknown game", "rank": r["Rank"], "image": bool(r["BG_ID"]),
             "off": r["before"] - r["price"], "off_pct": round(100 * (1 - r["price"] / r["before"])),
             "since": _day_label(r["before_day"])} for r in rows]


def _day_label(day):
    """'2026-11-06' -> '6 Nov'."""
    try:
        d = dt.date.fromisoformat(day)
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {d:%b}"


@source("bgf.market", ttl=600)
def market():
    # Cut-offs in Paris time, as BoardGameFinder writes its times (SQLite's 'localtime' would be the pod's UTC).
    since30 = str((dt.datetime.now(TZ) - dt.timedelta(days=30)).date())
    conn = db()
    try:
        live = conn.execute("SELECT COUNT(*) FROM Post WHERE Is_Online = 1").fetchone()[0]
        live_games = conn.execute("SELECT COUNT(DISTINCT BG_ID) FROM Post WHERE Is_Online = 1").fetchone()[0]
        tracked = conn.execute("SELECT COUNT(DISTINCT BG_Id) FROM Game_main_page_link WHERE COALESCE(Ignore, 0) = 0").fetchone()[0]
        per_day = conn.execute("""SELECT substr(Date, 1, 10) AS day, COUNT(DISTINCT Link) AS n FROM Price_History
                                  WHERE Date >= ? GROUP BY day ORDER BY day""", (since30,)).fetchall()
        last_scrape = conn.execute("SELECT MAX(Date) FROM Price_History").fetchone()[0]
        n_new = conn.execute("""SELECT COUNT(*) FROM Post WHERE Is_Online = 1 AND First_Seen LIKE '20%'
                                AND First_Seen >= ?""", (_since(1),)).fetchone()[0]
    finally:
        conn.close()
    # Every day of the last 30, a day without a scrape as None (a gap, not zero listings).
    seen = {r["day"]: r["n"] for r in per_day}
    today = dt.datetime.now(TZ).date()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    dr = drops.get(3).data or []
    pending = os.path.join(BGF_DIR, "Output", "review_pending.json")
    try:
        with open(pending, encoding="utf-8") as f:
            review = len(json.load(f))
    except (OSError, ValueError):
        review = None
    return {"live": live, "live_games": live_games, "tracked": tracked, "per_day": [(d, seen.get(d)) for d in days],
            "last_scrape": _paris(last_scrape), "n_new": n_new, "drops": dr[:30], "n_drops": len(dr),
            "review_pending": review, "missed_days": sum(1 for d in days[:-1] if d not in seen)}


@source("bgf.bundles", ttl=300)
def bundles():
    """Bundles, special editions and played legacy copies waiting for the LLM's judgement (review_pending.json),
    with what the listing says."""
    path = os.path.join(BGF_DIR, "Output", "review_pending.json")
    try:
        with open(path, encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return {"items": [], "kinds": {}, "file_time": None, "last_review": None}
    links = [p["link"] for p in pending if p.get("link")]
    conn = db()
    try:
        marks = ",".join("?" * len(links)) or "''"
        posts = {r["Link"]: dict(r) for r in conn.execute(
            f"""SELECT p.Link, p.Price, p.Condition, p.Location, p.Listing_Text, p.Seller_Description, p.First_Seen,
                       p.Is_Bundle, p.Is_Special_Edition, p.Is_Online, p.Can_Ship, p.BG_ID, p.Ad_Language,
                       COALESCE(g.Name, l.Name) AS name, g.Rank, g.Is_Legacy
                FROM Post p LEFT JOIN BGG_Games g ON g.ID = p.BG_ID
                LEFT JOIN (SELECT BG_Id, MIN(Name) AS Name FROM Game_main_page_link GROUP BY BG_Id) l ON l.BG_Id = p.BG_ID
                WHERE p.Link IN ({marks})""", links)}
    finally:
        conn.close()
    items, kinds = [], {}
    for p in pending:
        post = posts.get(p.get("link"))
        if not post or not post["Is_Online"]:
            continue
        text = " ".join(filter(None, [post["Listing_Text"], post["Seller_Description"]])).strip()
        # build_review_packet.py's three kinds; the last are plain-looking ads priced like a lot (find_hidden_lots)
        kind = ("Bundle" if post["Is_Bundle"] else "Special edition" if post["Is_Special_Edition"]
                else "Played legacy" if post["Is_Legacy"] else "Priced like a lot")
        kinds[kind] = kinds.get(kind, 0) + 1
        items.append({"link": post["Link"], "price": post["Price"], "name": post["name"] or "Unknown game",
                      "rank": post["Rank"], "kind": kind, "condition": post["Condition"] or "",
                      "language": post["Ad_Language"] or "", "place": _place(post["Location"]),
                      "ships": bool(post["Can_Ship"]), "text": text[:700] + ("…" if len(text) > 700 else ""),
                      "bg_id": post["BG_ID"], "image": bool(post["BG_ID"]), "first_seen": _paris(post["First_Seen"])})
    # What plain copies of the same game ask, to judge a lot against
    plain = {g["bg_id"]: g for g in games.get().data or []}
    for x in items:
        g = plain.get(x["bg_id"]) or {}
        x["plain_from"], x["plain_typical"] = g.get("cheapest"), g.get("typical")
    review = os.path.join(BGF_DIR, "Output", "bundle_newsletter.html")
    return {"items": items, "kinds": kinds, "file_time": os.path.getmtime(path),
            "last_review": os.path.getmtime(review) if os.path.exists(review) else None}


@source("bgf.games", ttl=600)
def games():
    """Every game BoardGameFinder follows, with what is for sale now: live listings, the cheapest copy and the
    typical ask (median) of the plain copies (no bundles, no expansions), and the usual price when known."""
    usual = dict((scored.get().data or {}).get("usual") or {})
    for d in (deals.get().data or {}).get("deals", []):
        usual.setdefault(d["bg_id"], d["baseline"])
    conn = db()
    try:
        followed = conn.execute("""SELECT l.BG_Id AS id, COALESCE(g.Name, MIN(l.Name)) AS name, g.French_Name AS fr,
                                          g.Rank, g.Year, g.Is_Legacy
                                   FROM Game_main_page_link l LEFT JOIN BGG_Games g ON g.ID = l.BG_Id
                                   WHERE COALESCE(l.Ignore, 0) = 0 GROUP BY l.BG_Id""").fetchall()
        posts = conn.execute("""SELECT BG_ID, Price, COALESCE(Is_Bundle, 0) OR COALESCE(Is_Extension, 0) AS other
                                FROM Post WHERE Is_Online = 1 AND COALESCE(Ignore, 0) = 0""").fetchall()
    finally:
        conn.close()
    live, asks = {}, {}
    for p in posts:
        live[p["BG_ID"]] = live.get(p["BG_ID"], 0) + 1
        if not p["other"] and p["Price"]:
            asks.setdefault(p["BG_ID"], []).append(p["Price"])
    out = []
    for g in followed:
        a = sorted(asks.get(g["id"], []))
        out.append({"bg_id": g["id"], "name": g["name"] or str(g["id"]), "fr": g["fr"] or "", "rank": g["Rank"],
                    "year": g["Year"], "legacy": bool(g["Is_Legacy"]), "image": bool(g["id"]),
                    "live": live.get(g["id"], 0), "cheapest": a[0] if a else None,
                    "typical": statistics.median(a) if a else None, "usual": usual.get(g["id"]),
                    # how far the cheapest copy is under the typical ask, when there are enough copies to say
                    "under": round(100 * (1 - a[0] / statistics.median(a))) if len(a) >= 3 else None})
    return out


@source("bgf.game", ttl=300)
def game(bg_id):
    valued = scored.get().data or {"by_link": {}, "usual": {}}
    conn = db()
    try:
        g = conn.execute("SELECT * FROM BGG_Games WHERE ID = ?", (bg_id,)).fetchone()
        page = conn.execute("""SELECT Link, Name FROM Game_main_page_link WHERE BG_Id = ?
                               ORDER BY COALESCE(Ignore, 0) LIMIT 1""", (bg_id,)).fetchone()
        if not g and not page:
            return None
        listings = [_listing(r, valued["by_link"]) for r in conn.execute(
            f"{POST} WHERE p.BG_ID = ? AND p.Is_Online = 1 ORDER BY p.Price", (bg_id,))]
        history = conn.execute("""SELECT substr(h.Date, 1, 10) AS day, MIN(h.Price) AS low, AVG(h.Price) AS avg,
                       COUNT(DISTINCT h.Link) AS n FROM Price_History h JOIN Post p ON p.Link = h.Link
                       WHERE p.BG_ID = ? AND COALESCE(p.Is_Bundle, 0) = 0 AND COALESCE(p.Is_Extension, 0) = 0
                       GROUP BY day ORDER BY day""", (bg_id,)).fetchall()
        since = str((dt.datetime.now(TZ) - dt.timedelta(days=60)).date())
        gone = [{"link": r["Link"], "price": r["Price"], "condition": r["Condition"] or "", "gone": _paris(r["Date_Last_Viewed"]),
                 "first_seen": _paris(r["First_Seen"]), "bundle": bool(r["Is_Bundle"])} for r in conn.execute(
            """SELECT Link, Price, Condition, Date_Last_Viewed, First_Seen, Is_Bundle FROM Post
               WHERE BG_ID = ? AND Is_Online = 0 AND Date_Last_Viewed >= ? AND COALESCE(Is_Extension, 0) = 0
               ORDER BY Date_Last_Viewed DESC LIMIT 12""", (bg_id, since))]
    finally:
        conn.close()
    plain = [x["price"] for x in listings if x["price"] and not x["bundle"] and not x["extension"]]
    hist = [dict(r) for r in history]
    month_ago = str((dt.datetime.now(TZ) - dt.timedelta(days=30)).date())
    low30 = min((h["low"] for h in hist if h["day"] >= month_ago and h["low"]), default=None)
    return {"bg_id": bg_id, "name": (g["Name"] if g else None) or page["Name"], "game": dict(g) if g else None,
            "okkazeo": page["Link"] if page else None, "image": True, "listings": listings,
            "cheapest": min(plain) if plain else None, "typical": statistics.median(plain) if plain else None,
            "usual": valued["usual"].get(bg_id), "low30": low30, "history": hist, "gone": gone}


LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (.*)$")


@source("bgf.run", ttl=120)
def run():
    """The last runs (Kubernetes), each step's failure streak, and the latest deal_finder log run."""
    state_path = os.path.join(BGF_DIR, "pipeline_state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    steps = [{"key": k, "label": label, "critical": crit, "llm": k in LLM_STEPS,
              "failures": (state.get(k) or {}).get("consecutive_failures", 0)} for k, label, crit in STEPS]
    log_path = os.path.join(BGF_DIR, "Output", "log.txt")
    last_run = []
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-300:]
        start = max((i for i, l in enumerate(lines) if "NEW RUN" in l), default=0)
        for l in lines[start + 1:]:
            m = LOG_LINE.match(l.strip())
            if m:
                last_run.append({"at": m.group(1), "text": m.group(2)})
    runs = k8s.runs_of("boardgamefinder")
    # Step by step for the newest run, from its own log (live while the pod exists, else what the lab saved).
    detail = None
    if runs:
        pods = k8s.pods_of_job(runs[0]["name"])
        if pods:
            detail = logs.bgf(k8s.log_tail.get("apps", pods[0]["name"], 20000).data or "")
    if not detail or not detail["steps"]:
        saved = store.job_logs("boardgamefinder", days=30)
        detail = saved[0]["summary"] if saved else None
    return {"steps": steps, "detail": detail, "deal_log": last_run, "runs": runs,
            "cron": ((k8s.jobs.get().data or {}).get("cronjobs") or {}).get("boardgamefinder")}
