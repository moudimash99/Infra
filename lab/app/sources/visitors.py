"""Anyone's matches: a visitor gives a BGG username, the lab fetches the games they rated from BGG's XML API
(token in BGG_TOKEN, Secret tools/lab-bgg), scores every host against them with bg-taste-match's score.py (a child
process on the read-only database, taste.run_score) and keeps the result in lab.db for a day.

One job at a time (each runs score.py's bootstraps for every host, on a machine whose memory is booked), at most
QUEUE_MAX waiting, and at most PER_HOUR new names an hour from the whole site. bgtaste.db is never written: the
visitor's ratings live only in lab.db, next to their result.
"""
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .. import store
from . import taste

log = logging.getLogger("lab")
API = "https://boardgamegeek.com/xmlapi2/collection"
USER_AGENT = "TasteMatch/1.0 (board game reviewer matching)"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\- ]{1,39}")     # BGG usernames: letters, digits, _ . - and spaces
KEEP_S = 86400              # a result is reused for a day
QUEUE_MAX = 20
PER_HOUR = 60
SCHEMA = """CREATE TABLE IF NOT EXISTS visitors (key TEXT PRIMARY KEY, name TEXT, state TEXT, step TEXT, error TEXT,
            asked REAL, done REAL, rated INTEGER, ratings TEXT, result TEXT)"""


class VisitorError(Exception):
    """Something to tell the visitor in plain words."""


def key_of(name):
    return " ".join(name.split()).lower()


def valid(name):
    return bool(name) and bool(NAME.fullmatch(" ".join(name.split())))


# ---- BGG ------------------------------------------------------------------------------------------------------------

def _get(params, tries=8):
    token = os.environ.get("BGG_TOKEN", "")
    if not token:
        raise VisitorError("The connection to BoardGameGeek isn't set up yet.")
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                if r.status == 202:          # BGG queues collection requests: ask again shortly
                    time.sleep(3 * (attempt + 1))
                    continue
            return ET.fromstring(body)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(6 * (attempt + 1))
                continue
            raise VisitorError(f"BoardGameGeek answered {e.code}.") from e
        except (urllib.error.URLError, TimeoutError, ET.ParseError):
            time.sleep(5 * (attempt + 1))
    raise VisitorError("BoardGameGeek is busy right now. Try again in a few minutes.")


def parse_ratings(root):
    """A collection's XML -> {bgg_id: rating out of 10} for the items the user rated."""
    if root.tag == "errors":
        msg = root.findtext("error/message") or ""
        raise VisitorError("There is no BoardGameGeek user with that name." if "invalid username" in msg.lower()
                           else f"BoardGameGeek: {msg or 'an error'}")
    out = {}
    for it in root.findall("item"):
        node = it.find("stats/rating")
        try:
            value = float(node.get("value")) if node is not None else None
        except (TypeError, ValueError):
            value = None                     # "N/A": not rated
        if value:
            out[int(it.get("objectid"))] = value
    return out


def fetch_ratings(name):
    """Games and expansions the user rated (BGG wants them asked for separately)."""
    ratings = {}
    for params in ({"subtype": "boardgame", "excludesubtype": "boardgameexpansion"}, {"subtype": "boardgameexpansion"}):
        ratings.update(parse_ratings(_get({"username": name, "rated": 1, "stats": 1, **params})))
    return ratings


# ---- the queue ------------------------------------------------------------------------------------------------------

_q = queue.Queue()
_lock = threading.Lock()
_worker = None
_recent = []            # times of new requests, for PER_HOUR
_parsed = {}            # key -> (done, built result): the pages read the same result many times


def _db():
    c = store.conn()
    c.execute(SCHEMA)
    return c


def _set(key, **cols):
    with _db() as c:
        sets = ", ".join(f"{k} = ?" for k in cols)
        c.execute(f"UPDATE visitors SET {sets} WHERE key = ?", (*cols.values(), key))


def status(name):
    """The visitor's row as a dict (without the big columns), or None."""
    with _db() as c:
        r = c.execute("SELECT key, name, state, step, error, asked, done, rated FROM visitors WHERE key = ?",
                      (key_of(name),)).fetchone()
    if not r:
        return None
    out = dict(r)
    if out["state"] == "queued":
        out["ahead"] = _q.qsize()
    return out


def result(name):
    """The built result (as taste.build makes it) for a finished name, cached in memory."""
    key = key_of(name)
    with _db() as c:
        r = c.execute("SELECT done, result, name FROM visitors WHERE key = ? AND state = 'done' AND done >= ?",
                      (key, time.time() - KEEP_S)).fetchone()
    if not r or not r["result"]:
        return None
    hit = _parsed.get(key)
    if hit and hit[0] == r["done"]:
        return hit[1]
    built = taste.build(json.loads(r["result"]), r["name"])
    built["at"] = r["done"]
    if len(_parsed) > 30:
        _parsed.clear()
    _parsed[key] = (r["done"], built)
    return built


def purge():
    """Forget visitors after KEEP_S: their BGG ratings and result go, as the pages say."""
    with _db() as c:
        c.execute("""DELETE FROM visitors WHERE state NOT IN ('queued', 'working')
                     AND COALESCE(done, asked, 0) < ?""", (time.time() - KEEP_S,))


def ask(name):
    """Start (or reuse) a match for name. Returns the status; raises VisitorError when it can't be taken now."""
    name = " ".join(name.split())
    if not valid(name):
        raise VisitorError("That doesn't look like a BoardGameGeek username.")
    key, now = key_of(name), time.time()
    with _lock:
        purge()
        st = status(name)
        if st and (st["state"] in ("queued", "working") or (st["state"] == "done" and now - (st["done"] or 0) < KEEP_S)):
            return st
        _recent[:] = [t for t in _recent if now - t < 3600]
        if len(_recent) >= PER_HOUR or _q.qsize() >= QUEUE_MAX:
            raise VisitorError("Lots of people are matching right now. Try again in a few minutes.")
        _recent.append(now)
        with _db() as c:
            c.execute("""INSERT INTO visitors (key, name, state, step, error, asked) VALUES (?, ?, 'queued', '', '', ?)
                         ON CONFLICT(key) DO UPDATE SET name = excluded.name, state = 'queued', step = '', error = '',
                         asked = excluded.asked""", (key, name, now))
        _q.put(key)
        _start()
    return status(name)


def _start():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_work, name="visitors", daemon=True)
        _worker.start()


def _work():
    while True:
        key = _q.get()
        try:
            run(key)
        except Exception:      # noqa: BLE001 - one visitor's failure must not stop the queue
            log.exception("visitor %s failed", key)
        finally:
            _q.task_done()


def run(key):
    with _db() as c:
        row = c.execute("SELECT name FROM visitors WHERE key = ?", (key,)).fetchone()
    if not row:
        return
    name = row["name"]
    try:
        _set(key, state="working", step="Asking BoardGameGeek for your ratings")
        ratings = fetch_ratings(name)
        if not ratings:
            raise VisitorError("This BoardGameGeek user hasn't rated any games yet (or the collection is private).")
        _set(key, step=f"Comparing your {len(ratings):,} ratings with every reviewer", rated=len(ratings),
             ratings=json.dumps(ratings))
        raw = taste.run_score(ratings=ratings, timeout=300)
        if not raw.get("user"):
            raise VisitorError("None of the games you rated have reviews here yet.")
        _set(key, state="done", step="", done=time.time(), result=json.dumps(raw))
    except VisitorError as e:
        _set(key, state="error", step="", error=str(e))
    except Exception as e:     # noqa: BLE001
        log.warning("visitor %s: %s", name, e)
        _set(key, state="error", step="", error="Something went wrong on our side. Try again in a while.")


def resume():
    """After a restart: forget what is over a day old, put back what was waiting or half done."""
    purge()
    with _db() as c:
        keys = [r[0] for r in c.execute("SELECT key FROM visitors WHERE state IN ('queued', 'working') ORDER BY asked")]
    for k in keys:
        _set(k, state="queued", step="")
        _q.put(k)
    if keys:
        _start()
