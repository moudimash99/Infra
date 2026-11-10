"""Taste Match's results: which hosts rate games the way the user does, and the games and verdicts behind it.

The match is bg-taste-match's own score.py (stage 7), run in a child process with a read-only connection, at most
every 10 minutes while people look (the page gets the last result at once; a fresh one is made behind it). A failed
run keeps the last good result. Everything else is read from bgtaste.db, opened read-only.

Scales: hosts' verdicts are out of 5; BGG ratings and averages are out of 10 and shown divided by 2 (SITE_NOTES.md).
"""
import html
import json
import logging
import os
import subprocess
import sys
import threading
import time

from . import Reading, source
from .bgt import BGT_DIR, CATEGORY_SQL, FETCH_CATEGORIES, channel_info, db

log = logging.getLogger("lab.taste")
USER = os.environ.get("TM_USER", "moudimash99")
MIN_SHARED = 5           # as build_results.py: fewer shared games than this and a host is listed, not ranked
REFRESH = 600
REJECTED = ("mention", "someone_else", "voice_conflict")    # verdicts the checks found weren't the host's own
ACCEPTED_SQL = "COALESCE(r.attribution, '') NOT IN ('mention', 'someone_else', 'voice_conflict')"

# Runs in the child: score.py from the pipeline's own folder, on a connection that can only read.
_SCRIPT = r'''
import json, sqlite3, sys
sys.dont_write_bytecode = True
folder, user = sys.argv[1], sys.argv[2]
sys.path.insert(0, folder)
import score
conn = sqlite3.connect("file:" + folder + "/bgtaste.db?mode=ro", uri=True, timeout=20)
conn.row_factory = sqlite3.Row
results, mine = score.score_all(conn, user)
keep = ("id", "name", "n", "rated", "corr", "match", "low", "high")
json.dump({"user": {str(g): v for g, v in mine.items()},
           "ures": {str(g): v for g, v in (results[0]["ures"] if results else {}).items()},
           "reviewers": [dict({k: r[k] for k in keep}, rres={str(g): v for g, v in r["rres"].items()},
                              points={str(g): v for g, v in r["points"].items()}) for r in results]}, sys.stdout)
'''


def nice_name(name):
    """Names typed in lower case by the pipeline ('paul') read as names ('Paul')."""
    return name.capitalize() if name and name.islower() else name


def label(r):
    """Plain words for a host's match: (text, badge class)."""
    if r["n"] < MIN_SHARED:
        return "Too few shared games", ""
    m = r["match"]
    if m >= 0.3:
        return "Strong match", "solid"
    if m >= 0.15:
        return "Good match", "accent"
    if m >= 0.05:
        return "Slight match", "accent"
    if m > -0.05:
        return "No clear pattern", ""
    if m > -0.15:
        return "Slightly different", "outline"
    return "Different taste", "outline"


SHORT = {"Strong match": "Strong", "Good match": "Good", "Slight match": "Slight", "No clear pattern": "Unclear",
         "Slightly different": "Slightly off", "Different taste": "Different"}


def _compute():
    folder = os.path.abspath(BGT_DIR)
    run = subprocess.run([sys.executable, "-c", _SCRIPT, folder, USER], capture_output=True, text=True, timeout=180,
                         cwd=folder, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if run.returncode != 0:
        lines = run.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"score.py exited with {run.returncode}")
    raw = json.loads(run.stdout)
    chans = channel_info.get().data or {}
    user = {int(g): tuple(v) for g, v in raw["user"].items()}
    reviewers = []
    for r in raw["reviewers"]:
        ch = r["id"].split(":")[0]
        r.update(name=nice_name(r["name"]), channel=ch, channel_name=(chans.get(ch) or {}).get("name", ch),
                 rres={int(g): v for g, v in r["rres"].items()},
                 points={int(g): tuple(v) for g, v in r["points"].items()}, ranked=r["n"] >= MIN_SHARED)
        r["label"], r["badge"] = label(r)
        r["short"] = SHORT.get(r["label"], r["label"])
        reviewers.append(r)
    ranked = [r for r in reviewers if r["ranked"]]
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return {"user": user, "ures": {int(g): v for g, v in raw["ures"].items()}, "reviewers": reviewers,
            "by_id": {r["id"]: r for r in reviewers}, "ranked": ranked, "at": time.time(), "username": USER}


class _Scores:
    """The latest match result. The first read waits for it; later reads get it at once and, once it is older than
    REFRESH, start a new run in the background."""

    def __init__(self):
        self.reading = None
        self._lock = threading.Lock()
        self._busy = False

    def _run(self):
        prev = self.reading
        try:
            self.reading = Reading(True, _compute(), "", time.time())
        except Exception as e:      # noqa: BLE001 - kept with the last good result, shown as a warning
            log.warning("taste scores failed: %s", e)
            self.reading = Reading(False, prev.data if prev else None, f"{type(e).__name__}: {e}"[:300], time.time())

    def _background(self):
        try:
            with self._lock:
                self._run()
        finally:
            self._busy = False

    def get(self):
        r = self.reading
        if r is None or (r.data is None and time.time() - r.read_at > 60):
            with self._lock:
                if self.reading is r:
                    self._run()
            return self.reading
        if time.time() - r.read_at > REFRESH and not self._busy:
            self._busy = True
            threading.Thread(target=self._background, daemon=True).start()
        return r


scores = _Scores()


# ---- what the match says ------------------------------------------------------------------------------------

def shared_lists(s, r, n=6):
    """Where the user and host r agree and disagree most (by taste signal: both sides' distance from BGG's average
    pointing the same way or not), and what they love that the user hasn't rated."""
    ures, rres = s["ures"], r["rres"]
    shared = [g for g in rres if g in ures]
    shared.sort(key=lambda g: ures[g] * rres[g])
    agree = [g for g in reversed(shared) if ures[g] * rres[g] > 0][:n]
    disagree = [g for g in shared if ures[g] * rres[g] < 0][:n]
    loves = sorted((g for g in rres if g not in s["user"] and r["points"][g][0] >= 4), key=lambda g: -rres[g])
    return agree, disagree, loves


def recommendations(s, skip=(), limit=None):
    """Games the user hasn't rated that hosts who match them (ranked, match above 0) score 4 or more: the more such
    hosts and the closer they match, the higher. -> [{gid, score (their mean), fans: [(host, score)], weight}]"""
    games = {}
    for r in s["ranked"]:
        if r["match"] <= 0:
            continue
        for g, (score, _avg, _w) in r["points"].items():
            if g in s["user"] or g in skip or score < 4:
                continue
            e = games.setdefault(g, {"gid": g, "fans": [], "weight": 0.0})
            e["fans"].append((r, score))
            e["weight"] += r["match"] * (score - 3)
    out = sorted(games.values(), key=lambda e: (-e["weight"], -len(e["fans"])))
    for e in out:
        e["fans"].sort(key=lambda f: -f[0]["match"])
        e["score"] = sum(sc for _, sc in e["fans"]) / len(e["fans"])
    return out[:limit] if limit else out


def matches_say(s, gid):
    """The game's score from the hosts who match the user, weighted by how closely (None when none rated it)."""
    num = den = 0.0
    for r in s["ranked"]:
        if r["match"] > 0 and gid in r["points"]:
            num += r["match"] * r["points"][gid][0]
            den += r["match"]
    return num / den if den else None


# ---- from the database ------------------------------------------------------------------------------------------

@source("taste.profile", ttl=600)
def profile():
    """The user's BGG games by canonical id: rating (out of 10), owned, wishlist."""
    conn = db()
    try:
        rows = conn.execute("""SELECT COALESCE(c.canonical_id, u.bgg_id) AS gid, AVG(u.rating) AS rating,
                                      MAX(u.own) AS own, MAX(u.prev_own) AS prev, MAX(u.wishlist) AS wish
                               FROM user_games u LEFT JOIN bgg_canonical c ON c.id = u.bgg_id
                               WHERE u.username = ? GROUP BY gid""", (USER,)).fetchall()
    finally:
        conn.close()
    games = {r["gid"]: dict(r) for r in rows}
    return {"games": games, "rated": sum(1 for g in games.values() if g["rating"] is not None),
            "owned": {g for g, v in games.items() if v["own"] or v["prev"]},
            "wishlist": {g for g, v in games.items() if v["wish"]}}


@source("taste.things", ttl=1800)
def things():
    """Every BGG game the pipeline knows: {id: {name, year, rank, average, average_weight, players, time}}."""
    conn = db()
    try:
        return {r["id"]: dict(r) for r in conn.execute(
            """SELECT id, name, year, rank, average, average_weight, min_players, max_players, playing_time
               FROM bgg_things""")}
    finally:
        conn.close()


@source("taste.canonical", ttl=1800)
def canonical():
    """Editions that count as one game: {edition id: the game's id} (bgg_canonical)."""
    conn = db()
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, canonical_id FROM bgg_canonical WHERE canonical_id IS NOT NULL")}
    finally:
        conn.close()


@source("taste.games", ttl=300)
def games():
    """Every game with an accepted verdict: {gid: {name, year, rank, average, ..., verdicts, hosts, avg}}."""
    info = things.get().data or {}
    conn = db()
    try:
        rows = conn.execute(f"""SELECT COALESCE(c.canonical_id, g.bgg_id) AS gid, r.reviewer_id AS rid, r.score
                                FROM ratings r JOIN games g ON g.name = r.game
                                LEFT JOIN bgg_canonical c ON c.id = g.bgg_id
                                WHERE g.bgg_id IS NOT NULL AND {ACCEPTED_SQL}""").fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        if r["gid"] not in info:
            continue
        e = out.setdefault(r["gid"], {**info[r["gid"]], "gid": r["gid"], "scores": [], "hosts": set()})
        e["scores"].append(r["score"])
        e["hosts"].add(r["rid"])
    for e in out.values():
        e["verdicts"] = len(e["scores"])
        e["avg"] = sum(e["scores"]) / len(e["scores"])
    return out


@source("taste.game", ttl=300)
def game(gid):
    """One game: BGG's facts and every verdict on it (any edition)."""
    conn = db()
    try:
        g = conn.execute("""SELECT id, name, year, rank, average, average_weight, min_players, max_players,
                                   playing_time, record FROM bgg_things WHERE id = ?""", (gid,)).fetchone()
        if not g:
            return None
        verdicts = [dict(r) for r in conn.execute(f"""
            SELECT r.video_id, r.reviewer_id AS rid, r.score, r.confidence, r.quote, r.attribution, r.rated_at,
                   w.name AS host, w.channel, v.title, v.duration
            FROM ratings r JOIN games g ON g.name = r.game LEFT JOIN bgg_canonical c ON c.id = g.bgg_id
            JOIN reviewers w ON w.id = r.reviewer_id JOIN videos v ON v.id = r.video_id
            WHERE COALESCE(c.canonical_id, g.bgg_id) = ? AND {ACCEPTED_SQL}
            ORDER BY r.score DESC""", (gid,))]
    finally:
        conn.close()
    rec = json.loads(g["record"] or "{}")
    for v in verdicts:
        v["host"] = nice_name(v["host"])
    return {**{k: g[k] for k in g.keys() if k != "record"}, "verdicts": verdicts,
            "description": html.unescape(rec.get("description") or "").strip(), "designers": rec.get("designers") or [],
            "mechanics": rec.get("mechanics") or [], "categories": rec.get("categories") or [],
            "min_time": rec.get("min_playing_time"), "max_time": rec.get("max_playing_time"),
            "min_age": rec.get("min_age"), "users_rated": rec.get("users_rated")}


def image_url(gid):
    """The game's full box art on BGG (the lab shrinks it), else its small thumbnail."""
    conn = db()
    try:
        r = conn.execute("""SELECT json_extract(record, '$.image'), json_extract(record, '$.thumbnail')
                            FROM bgg_things WHERE id = ?""", (gid,)).fetchone()
    finally:
        conn.close()
    return (r[0] or r[1]) if r else None


@source("taste.host", ttl=300)
def host_verdicts(rid):
    """Every accepted verdict by one host, best first."""
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute(f"""
            SELECT r.video_id, r.score, r.confidence, r.quote, r.rated_at, r.game,
                   COALESCE(c.canonical_id, g.bgg_id) AS gid, b.name AS game_name, v.title
            FROM ratings r JOIN videos v ON v.id = r.video_id
            LEFT JOIN games g ON g.name = r.game LEFT JOIN bgg_canonical c ON c.id = g.bgg_id
            LEFT JOIN bgg_things b ON b.id = COALESCE(c.canonical_id, g.bgg_id)
            WHERE r.reviewer_id = ? AND {ACCEPTED_SQL}
            ORDER BY r.score DESC, COALESCE(b.name, r.game)""", (rid,))]
        who = conn.execute("SELECT id, channel, name FROM reviewers WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()
    return {"verdicts": rows, "host": dict(who) if who else None}


@source("taste.stats", ttl=300)
def stats():
    conn = db()
    try:
        verdicts, videos = conn.execute(f"""SELECT COUNT(*), COUNT(DISTINCT r.video_id) FROM ratings r
                                            JOIN reviewers w ON w.id = r.reviewer_id
                                            WHERE w.name NOT LIKE 'unknown%' AND {ACCEPTED_SQL}""").fetchone()
        week = conn.execute(f"""SELECT COUNT(*) FROM ratings r WHERE {ACCEPTED_SQL}
                                AND r.rated_at >= datetime('now', '-7 days')""").fetchone()[0]
        channels = conn.execute("SELECT COUNT(DISTINCT v.channel) FROM ratings r JOIN videos v ON v.id = r.video_id"
                                ).fetchone()[0]
    finally:
        conn.close()
    return {"verdicts": verdicts, "videos": videos, "week": week, "channels": channels}


@source("taste.recent", ttl=120)
def recent(limit=8):
    """The reviews rated last: video, channel, how many verdicts and on which games."""
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute(f"""
            SELECT r.video_id AS id, MAX(r.rated_at) AS at, COUNT(*) AS n, GROUP_CONCAT(DISTINCT r.game) AS games,
                   v.title, v.channel, v.duration
            FROM ratings r JOIN videos v ON v.id = r.video_id WHERE {ACCEPTED_SQL}
            GROUP BY r.video_id ORDER BY at DESC LIMIT ?""", (limit,))]
    finally:
        conn.close()
    names = channel_info.get().data or {}
    for r in rows:
        r["channel_name"] = (names.get(r["channel"]) or {}).get("name", r["channel"])
        r["games"] = (r["games"] or "").split(",")
    return rows


VIDEO_PAGE = 30


@source("taste.videos", ttl=60)
def videos(show="rated", channel="", q="", page=1):
    """A page of the round's videos: show 'rated', 'waiting' (in the round, not rated yet) or 'all' (the round);
    q finds a title or a game named in a verdict. -> {rows, total}"""
    marks = ",".join("?" * len(FETCH_CATEGORIES))
    where, args = [f"{CATEGORY_SQL} IN ({marks})"], list(FETCH_CATEGORIES)
    if show == "rated":
        where.append("v.id IN (SELECT video_id FROM ratings)")
    elif show == "waiting":
        where.append("v.id NOT IN (SELECT video_id FROM ratings) AND v.id NOT IN (SELECT video_id FROM rated_parts)")
    if channel:
        where.append("v.channel = ?")
        args.append(channel)
    if q:
        where.append("(v.title LIKE ? OR v.id IN (SELECT video_id FROM ratings WHERE game LIKE ?))")
        args += [f"%{q}%", f"%{q}%"]
    sql_where = " AND ".join(where)
    order = "last_rated DESC" if show == "rated" else "v.rowid DESC"
    conn = db()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM videos v WHERE {sql_where}", args).fetchone()[0]
        rows = [dict(r) for r in conn.execute(f"""
            SELECT v.id, v.title, v.channel, v.duration, v.timestamp, v.view_count,
                   (SELECT COUNT(*) FROM ratings r WHERE r.video_id = v.id AND {ACCEPTED_SQL}) AS n,
                   (SELECT MAX(rated_at) FROM ratings r WHERE r.video_id = v.id) AS last_rated,
                   EXISTS(SELECT 1 FROM transcripts t WHERE t.video_id = v.id) AS has_text,
                   EXISTS(SELECT 1 FROM speaker_transcripts s WHERE s.video_id = v.id) AS has_voices
            FROM videos v WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
            args + [VIDEO_PAGE, (page - 1) * VIDEO_PAGE])]
    finally:
        conn.close()
    return {"rows": rows, "total": total}
