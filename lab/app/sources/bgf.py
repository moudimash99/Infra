"""BoardGameFinder: today's deals, what's new, price drops, the market, and how the last run went.

Reads /srv/apps/boardgamefinder/data read-only: Games.db (opened mode=ro), Output/deals.csv, Output/log.txt,
pipeline_state.json. Never the repo itself (it holds credentials).
"""
import csv
import datetime as dt
import json
import os
import re
import sqlite3

from . import source, k8s
from .. import logs, store
from ..config import BGF_DIR, TZ

STEPS = [  # run_pipeline.py order; critical steps stop the run
    ("top_500_games", "BGG top 500", False), ("update_all_ranks", "BGG ranks", False),
    ("bgg_hotness", "BGG hotness", False), ("fetch_legacy_flags", "Legacy flags", False),
    ("Okkazeo_games_finder", "Find game pages", False), ("price_finder", "Scrape listings", True),
    ("fetch_descriptions", "Seller descriptions", False), ("classify_postings_with_llm", "Classify listings (LLM)", False),
    ("deal_finder", "Find deals + email", True), ("build_review_packet", "Bundle packet", False),
    ("llm_review", "Bundle review (LLM)", False),
]


def db():
    conn = sqlite3.connect(f"file:{os.path.join(BGF_DIR, 'Games.db')}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _images(conn):
    img = {r[0]: r[1] for r in conn.execute("SELECT BG_Id, Image_URL FROM Game_main_page_link WHERE Image_URL != ''")}
    img.update({r[0]: r[1] for r in conn.execute("SELECT ID, Image_URL FROM BGG_Games WHERE Image_URL != ''")})
    return img


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


@source("bgf.deals", ttl=300)
def deals():
    path = os.path.join(BGF_DIR, "Output", "deals.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8"))) if os.path.exists(path) else []
    conn = db()
    try:
        img = _images(conn)
        alerted = {r[0]: r[1] for r in conn.execute("SELECT Link, Alerted_At FROM Post WHERE Alerted_At IS NOT NULL")}
        first = {r[0]: r[1] for r in conn.execute("SELECT Link, First_Seen FROM Post WHERE First_Seen LIKE '20%'")}
    finally:
        conn.close()
    out = []
    for r in rows:
        bg = int(_f(r.get("BG_ID")) or 0)
        out.append({"name": r["Name"], "price": _f(r["Price"]), "baseline": _f(r["Baseline"]), "edge": _f(r["Edge"]),
                    "net": _f(r["Net"]), "comps": int(_f(r.get("Sold_Comps")) or 0), "condition": r.get("Condition", ""),
                    "language": r.get("Language", ""), "bundle": r.get("Bundle", ""), "ships": r.get("Ships", ""),
                    "link": r["Link"], "posted": r.get("Post_Date", ""), "bg_id": bg, "image": img.get(bg),
                    "alerted": _paris(alerted.get(r["Link"])), "first_seen": _paris(first.get(r["Link"]))})
    return {"deals": out, "file_time": os.path.getmtime(path) if os.path.exists(path) else None}


@source("bgf.market", ttl=600)
def market():
    # Cut-offs in Paris time, as BoardGameFinder writes its times (SQLite's 'localtime' would be the pod's UTC).
    now = dt.datetime.now(TZ)
    day_ago = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    since30, since60, since3 = (str((now - dt.timedelta(days=n)).date()) for n in (30, 60, 3))
    conn = db()
    try:
        img = _images(conn)
        live = conn.execute("SELECT COUNT(*) FROM Post WHERE Is_Online = 1").fetchone()[0]
        tracked = conn.execute("SELECT COUNT(*) FROM Game_main_page_link WHERE COALESCE(Ignore, 0) = 0").fetchone()[0]
        per_day = conn.execute("""SELECT substr(Date, 1, 10) AS day, COUNT(DISTINCT Link) AS n FROM Price_History
                                  WHERE Date >= ? GROUP BY day ORDER BY day""", (since30,)).fetchall()
        last_scrape = conn.execute("SELECT MAX(Date) FROM Price_History").fetchone()[0]
        recent = "p.Is_Online = 1 AND p.First_Seen LIKE '20%' AND p.First_Seen >= ?"
        n_new = conn.execute(f"SELECT COUNT(*) FROM Post p WHERE {recent}", (day_ago,)).fetchone()[0]
        new = [dict(r) for r in conn.execute(f"""SELECT p.Link, p.Price, p.Condition, p.Location, p.First_Seen, p.BG_ID,
                       COALESCE(g.Name, l.Name) AS name FROM Post p
                       LEFT JOIN BGG_Games g ON g.ID = p.BG_ID
                       LEFT JOIN (SELECT BG_Id, MIN(Name) AS Name FROM Game_main_page_link GROUP BY BG_Id) l
                         ON l.BG_Id = p.BG_ID
                       WHERE {recent} ORDER BY p.First_Seen DESC LIMIT 60""", (day_ago,))]
        # Price drops: a listing's price on its latest scrape day below its price on the scrape day before (a day can
        # hold two runs, so prices are taken per day), latest day within 3 days.
        drops = [dict(r) for r in conn.execute("""
            WITH d AS (SELECT Link, substr(Date, 1, 10) AS day, MIN(Price) AS price FROM Price_History
                       WHERE Date >= ? GROUP BY Link, day),
                 r AS (SELECT Link, day, price, ROW_NUMBER() OVER (PARTITION BY Link ORDER BY day DESC) AS k FROM d)
            SELECT a.Link, a.price, b.price AS before, a.day, b.day AS before_day, p.BG_ID, p.Condition,
                   COALESCE(g.Name, l.Name, '') AS name
            FROM r a JOIN r b ON b.Link = a.Link AND a.k = 1 AND b.k = 2
            JOIN Post p ON p.Link = a.Link
            LEFT JOIN BGG_Games g ON g.ID = p.BG_ID
            LEFT JOIN (SELECT BG_Id, MIN(Name) AS Name FROM Game_main_page_link GROUP BY BG_Id) l ON l.BG_Id = p.BG_ID
            WHERE a.price < b.price AND a.day >= ? AND p.Is_Online = 1
            ORDER BY (b.price - a.price) / b.price DESC""", (since60, since3))]
    finally:
        conn.close()
    for r in new + drops:
        r["image"] = img.get(r.get("BG_ID"))
        r["First_Seen"] = _paris(r.get("First_Seen")) if r.get("First_Seen") else None
    # Every day of the last 30, a day without a scrape as None (a gap, not zero listings).
    seen = {r["day"]: r["n"] for r in per_day}
    today = dt.datetime.now(TZ).date()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    pending = os.path.join(BGF_DIR, "Output", "review_pending.json")
    try:
        review = len(json.load(open(pending, encoding="utf-8")))
    except (OSError, ValueError):
        review = None
    return {"live": live, "tracked": tracked, "per_day": [(d, seen.get(d)) for d in days], "last_scrape": _paris(last_scrape),
            "new": new, "n_new": n_new, "drops": drops[:30], "n_drops": len(drops), "review_pending": review,
            "missed_days": sum(1 for d in days[:-1] if d not in seen)}


@source("bgf.game", ttl=300)
def game(bg_id):
    conn = db()
    try:
        g = conn.execute("SELECT * FROM BGG_Games WHERE ID = ?", (bg_id,)).fetchone()
        name = g["Name"] if g else (conn.execute("SELECT Name FROM Game_main_page_link WHERE BG_Id = ?", (bg_id,))
                                    .fetchone() or [str(bg_id)])[0]
        listings = [dict(r) for r in conn.execute("""SELECT Link, Price, Condition, Location, Can_Ship, Post_Date,
                       First_Seen, Is_Bundle, Is_Extension FROM Post WHERE BG_ID = ? AND Is_Online = 1
                       ORDER BY Price""", (bg_id,))]
        history = conn.execute("""SELECT substr(h.Date, 1, 10) AS day, MIN(h.Price) AS low, AVG(h.Price) AS avg,
                       COUNT(DISTINCT h.Link) AS n FROM Price_History h JOIN Post p ON p.Link = h.Link
                       WHERE p.BG_ID = ? AND COALESCE(p.Is_Bundle, 0) = 0 GROUP BY day ORDER BY day""",
                                  (bg_id,)).fetchall()
        img = _images(conn).get(bg_id)
    finally:
        conn.close()
    return {"bg_id": bg_id, "name": name, "game": dict(g) if g else None, "image": img, "listings": listings,
            "history": [dict(r) for r in history]}


LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (.*)$")


@source("bgf.run", ttl=120)
def run():
    """The last runs (Kubernetes), each step's failure streak, and the latest deal_finder log run."""
    state_path = os.path.join(BGF_DIR, "pipeline_state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    steps = [{"key": k, "label": label, "critical": crit,
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
