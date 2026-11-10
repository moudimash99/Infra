"""The cats: the flash of the day, what Frigate saw, what the feeding monitor measured, and their weights.

Frigate (LAN only) is read through the lab, which proxies its images and clips. Its own detector finds few cat
visits, so the site also offers frames and short clips from the recordings at any time, and a frame from the
middle of each monitor run's window: the owner can see what the monitor's model was looking at.

The flash of the day is CATS_DIR/flash/<day>.mp4 with its poster (.jpg) and its moments (.json), built by the
cat-flash CronJob (Cat-Camera/daily_flash.py): at 19:20 the day so far, at 00:20 the whole of yesterday.
"""
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import time

import httpx

from . import source
from ..config import CAT_WEIGHT_DB, CATS_DIR, FRIGATE, FRIGATE_CAMERA, TZ

_http = httpx.Client(timeout=15)
CATS = ("Nala", "Simba")     # fixed order: Nala is colour slot 1, Simba slot 2, on every page and chart
FLASH = os.path.join(CATS_DIR, "flash")
DAY = re.compile(r"\d{4}-\d\d-\d\d")
KEEP_DAYS = 14               # Frigate keeps 14 days of recordings
DOING = {"moving": "On the move", "eating": "Eating", "feeding": "At the bowl", "drinking": "Drinking",
         "sleeping": "Asleep", "resting": "Resting", "playing": "Playing", "grooming": "Grooming"}


def cat_order(names):
    """Cats in the site's fixed order, others after."""
    names = list(dict.fromkeys(names))
    return [c for c in CATS if c in names] + sorted(c for c in names if c not in CATS)


def day_words(day, today=None):
    """'2026-11-10' -> ('Today', 'Tuesday 10 November', 'Tue 10 Nov')."""
    d = dt.date.fromisoformat(day)
    today = today or dt.datetime.now(TZ).date()
    near = {0: "Today", 1: "Yesterday"}.get((today - d).days)
    return near, f"{d:%A} {d.day} {d:%B}", f"{d:%a} {d.day} {d:%b}"


def date_words(ts, year=False):
    """An epoch -> '25 Oct' (or '25 Oct 2025')."""
    d = dt.datetime.fromtimestamp(ts, TZ)
    return f"{d.day} {d:%b}" + (f" {d.year}" if year else "")


def clock_len(seconds):
    """58.2 -> '0:58', 125 -> '2:05'."""
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


# ---- the flash of the day ------------------------------------------------------------------------------------

def flash_days():
    """Days with a built flash (video and moments both there), newest first."""
    try:
        names = set(os.listdir(FLASH))
    except FileNotFoundError:
        return []
    return sorted((n[:-5] for n in names if n.endswith(".json") and DAY.fullmatch(n[:-5]) and n[:-5] + ".mp4" in names),
                  reverse=True)


def flash(day):
    """One day's flash: its moments with their place in the video, or None."""
    if not DAY.fullmatch(day or ""):
        return None
    try:
        with open(os.path.join(FLASH, day + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if not os.path.exists(os.path.join(FLASH, day + ".mp4")):
        return None
    first, each = meta.get("title_seconds", 1.8), meta.get("clip_seconds", 4.0)
    items, per_cat = [], {}
    for i, it in enumerate(x for x in meta.get("items", []) if x.get("start")):
        at = it.get("at") or it["start"]
        cats = cat_order(it.get("cats") or [])
        for c in cats:
            per_cat[c] = per_cat.get(c, 0) + 1
        what = it.get("what") or ""
        items.append({**it, "cats": cats, "seek": first + i * each, "at": at,
                      "at_label": dt.datetime.fromtimestamp(at, TZ).strftime("%H:%M"),
                      "length": max(0, (it.get("end") or it["start"]) - it["start"]),
                      "what_label": DOING.get(what, what.capitalize() or "Seen"),
                      "in_video": clock_len(first + i * each)})
    near, long_, short = day_words(day)
    seconds = meta.get("seconds", 0)
    return {**meta, "day": day, "near": near, "day_long": long_, "day_label": short, "items": items,
            "moments": meta.get("moments", len(items)), "seconds": seconds, "length": clock_len(seconds),
            "cats": cat_order(per_cat), "per_cat": per_cat, "partial": bool(meta.get("partial")),
            "built": meta.get("built"),
            "title": ("Today so far" if near == "Today" and meta.get("partial") else near or long_)}


def flashes(limit=60):
    """Built flashes, newest first."""
    return [f for f in (flash(d) for d in flash_days()[:limit]) if f]


def cats_words(cats):
    """['Nala', 'Simba'] -> 'Nala and Simba'."""
    return " and ".join(cats) if len(cats) <= 2 else ", ".join(cats[:-1]) + " and " + cats[-1]


# ---- Frigate ---------------------------------------------------------------------------------------------------

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
        zones = e.get("zones") or []
        out.append({"id": e["id"], "start": e["start_time"], "end": e.get("end_time"), "zones": zones,
                    "where": "At the feeder" if "feeder_zone" in zones else "In the room",
                    "length": max(0, (e.get("end_time") or e["start_time"]) - e["start_time"]),
                    "score": data.get("top_score") or data.get("score"), "clip": e.get("has_clip"),
                    "snapshot": e.get("has_snapshot"), "sub_label": e.get("sub_label")})
    return sorted(out, key=lambda e: e["start"], reverse=True)


@source("cats.recordings", ttl=600)
def recordings():
    """Days with recordings (Frigate keeps 14), local time: {day: {hour: {events, motion, objects, seconds}}}."""
    r = _http.get(f"{FRIGATE}/api/{FRIGATE_CAMERA}/recordings/summary", params={"timezone": str(TZ)})
    r.raise_for_status()
    return {d["day"]: {int(h["hour"]): {"events": h.get("events", 0), "motion": h.get("motion", 0),
                                         "objects": h.get("objects", 0), "seconds": h.get("duration", 0)}
                       for h in d.get("hours", [])} for d in r.json()}


@source("cats.frigate", ttl=60)
def frigate():
    """Whether Frigate answers, and its version."""
    r = _http.get(f"{FRIGATE}/api/version", timeout=5)
    r.raise_for_status()
    return {"version": r.text.strip().strip('"')}


def hour_frames(day, now=None):
    """The top of each hour of a local day that has passed (frames from the recordings), as epoch seconds."""
    now = now or time.time()
    d = dt.date.fromisoformat(day)
    out = []
    for h in range(24):
        t = dt.datetime(d.year, d.month, d.day, h, tzinfo=TZ).timestamp()
        if now - KEEP_DAYS * 86400 < t < now - 60:
            out.append(int(t))
    return out


def recent_days(n=7, now=None):
    """The last n local days, newest first: [(day, label)] with 'Today', 'Yesterday', then 'Sun 8'."""
    today = dt.datetime.fromtimestamp(now or time.time(), TZ).date()
    out = []
    for i in range(n):
        d = today - dt.timedelta(days=i)
        out.append((d.isoformat(), {0: "Today", 1: "Yesterday"}.get(i) or f"{d:%a} {d.day}"))
    return out


# ---- the feeding monitor --------------------------------------------------------------------------------------

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
            with open(path) as f:
                d = json.load(f)
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
        r["middle"] = int((r["start"] + r["end"]) / 2) if r["start"] and r["end"] else None
    return runs[:limit]


# ---- weights ---------------------------------------------------------------------------------------------------

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


def weight_cards(data, now=None):
    """Per cat, in the site's order: the latest weigh-in, the change since the one before, and over about a year
    (from the last weigh-in at least 330 days before the latest, else the first one)."""
    now = now or time.time()
    cards = []
    for slot, cat in enumerate(cat_order(data), start=1):
        pts = sorted(data[cat])
        ts, kg = pts[-1]
        prev = pts[-2] if len(pts) > 1 else None
        base = next((p for p in reversed(pts) if p[0] <= ts - 330 * 86400), pts[0] if len(pts) > 1 else None)
        kgs = [p[1] for p in pts]
        a_year = base and ts - base[0] >= 330 * 86400
        cards.append({"cat": cat, "slot": slot, "kg": kg, "ts": ts, "points": pts, "n": len(pts),
                      "prev": prev, "d_prev": (kg - prev[1]) if prev else None,
                      "prev_words": f"since {date_words(prev[0])}" if prev else "",
                      "base": base, "d_base": (kg - base[1]) if base else None,
                      "base_words": ("in a year" if a_year else f"since {date_words(base[0], year=True)}") if base else "",
                      "low": min(kgs), "high": max(kgs), "age_days": (now - ts) / 86400})
    return cards


def weight_table(data):
    """Weigh-ins by day, newest first: [{day, label, ts, Nala: kg, Simba: kg}] (both cats are usually weighed
    together)."""
    rows = {}
    for cat, pts in data.items():
        for ts, kg in pts:
            day = dt.datetime.fromtimestamp(ts, TZ).date().isoformat()
            row = rows.setdefault(day, {"day": day, "ts": ts, "label": date_words(ts, year=True)})
            row[cat] = kg
            row["ts"] = max(row["ts"], ts)
    return sorted(rows.values(), key=lambda r: r["day"], reverse=True)


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
