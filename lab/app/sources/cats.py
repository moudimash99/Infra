"""The cats: what Frigate saw at the feeder, what the feeding monitor measured, and their weights.

Frigate (LAN only) is read through the lab, which proxies its images and clips. Its own detector finds few cat
visits, so the page also offers frames and short clips from the recordings at any time, and a frame from the
middle of each monitor run's window: the owner can see what the monitor's model was looking at.
"""
import datetime as dt
import glob
import json
import os
import sqlite3
import time

import httpx

from . import source
from ..config import CAT_WEIGHT_DB, CATS_DIR, FRIGATE, FRIGATE_CAMERA, TZ

_http = httpx.Client(timeout=15)
CATS = ("Simba", "Nala")


@source("cats.events", ttl=120)
def events(days=14):
    """Frigate's cat events, newest first."""
    after = int(time.time() - days * 86400)
    r = _http.get(f"{FRIGATE}/api/events", params={"camera": FRIGATE_CAMERA, "label": "cat", "after": after,
                                                    "limit": 2000})
    r.raise_for_status()
    out = []
    for e in r.json():
        data = e.get("data") or {}
        out.append({"id": e["id"], "start": e["start_time"], "end": e.get("end_time"), "zones": e.get("zones") or [],
                    "score": data.get("top_score") or data.get("score"), "clip": e.get("has_clip"),
                    "snapshot": e.get("has_snapshot"), "sub_label": e.get("sub_label")})
    return sorted(out, key=lambda e: e["start"], reverse=True)


def per_day(evts, days=14):
    today = dt.datetime.now(TZ).date()
    counts = {today - dt.timedelta(days=i): 0 for i in range(days - 1, -1, -1)}
    for e in evts:
        d = dt.datetime.fromtimestamp(e["start"], TZ).date()
        if d in counts:
            counts[d] += 1
    return list(counts.items())


def per_hour(evts):
    hours = [0] * 24
    for e in evts:
        hours[dt.datetime.fromtimestamp(e["start"], TZ).hour] += 1
    return hours


@source("cats.recordings", ttl=600)
def recordings():
    """Days with recordings (Frigate keeps 14): [{day, hours: [...]}]."""
    r = _http.get(f"{FRIGATE}/api/{FRIGATE_CAMERA}/recordings/summary")
    r.raise_for_status()
    return r.json()


def _local(s):
    """'2026-11-10 02:17:27.918258' (the monitor writes local time) -> epoch."""
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()
    except (TypeError, ValueError):
        return None


@source("cats.monitor", ttl=300)
def monitor(limit=30):
    """The monitor's runs, newest first, from each run's own JSON report (the history file lacks presence)."""
    runs = []
    for path in glob.glob(os.path.join(CATS_DIR, "rolling_runs", "rolling_*", "feeding_report_*.json")):
        try:
            d = json.load(open(path))
        except (OSError, ValueError):
            continue
        start, end = _local(d.get("start_local")), _local(d.get("end_local"))
        runs.append({"block": d.get("block_name") or os.path.basename(os.path.dirname(path)), "start": start,
                     "end": end, "presence": d.get("cat_presence_time") or {}, "feeding": d.get("cat_feeding_time") or {},
                     "sessions": d.get("cat_session_counts") or {}, "events": d.get("events") or [],
                     "movements": d.get("feeder_movements_detected", 0)})
    runs.sort(key=lambda r: r["start"] or 0, reverse=True)
    for r in runs:
        window = (r["end"] or 0) - (r["start"] or 0)
        # Present for (nearly) the whole window means the model is seeing a cat that isn't moving: often a false
        # detection (a cushion, a reflection) or a cat asleep in view. Worth a look at the frame.
        r["window"] = window
        r["suspicious"] = [c for c, s in r["presence"].items() if window and s >= 0.9 * window]
    return runs[:limit]


@source("cats.weights", ttl=600)
def weights():
    conn = sqlite3.connect(f"file:{CAT_WEIGHT_DB}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute("SELECT cat_name, date_str, weight FROM weights ORDER BY date_str").fetchall()
    finally:
        conn.close()
    out = {}
    for cat, when, kg in rows:
        try:
            ts = dt.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
        except ValueError:
            continue
        out.setdefault(cat, []).append((ts, kg))
    return out


# Proxied media (paths only from these builders: the event id and times are checked by the routes).
def event_snapshot_url(event_id):
    return f"{FRIGATE}/api/events/{event_id}/snapshot.jpg"


def event_clip_url(event_id):
    return f"{FRIGATE}/api/events/{event_id}/clip.mp4"


def frame_url(ts):
    return f"{FRIGATE}/api/{FRIGATE_CAMERA}/recordings/{int(ts)}/snapshot.jpg"


def latest_url():
    return f"{FRIGATE}/api/{FRIGATE_CAMERA}/latest.jpg"


def range_clip_url(start, end):
    return f"{FRIGATE}/api/{FRIGATE_CAMERA}/start/{int(start)}/end/{int(end)}/clip.mp4"
