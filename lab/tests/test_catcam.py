"""Cat cam: the flash files, the weight cards and chart scale, job schedules, and every page rendered without Frigate
or Kubernetes. Run from Infra/lab: PYTHONPATH=. python -m pytest -q tests/test_catcam.py"""
import datetime as dt
import json
import time

import pytest

from app.config import TZ
from app.sources import Reading, cats


def _write_flash(folder, day, items, partial=False, video=True):
    folder.mkdir(exist_ok=True)
    (folder / f"{day}.json").write_text(json.dumps({"day": day, "partial": partial, "title_seconds": 1.8,
                                                    "clip_seconds": 4.0, "built": time.time() - 600,
                                                    "moments": len(items), "seconds": 1.8 + 4 * len(items),
                                                    "items": items}))
    if video:
        (folder / f"{day}.mp4").write_bytes(b"\0")
        (folder / f"{day}.jpg").write_bytes(b"\0")


@pytest.fixture
def flash_dir(tmp_path, monkeypatch):
    folder = tmp_path / "flash"
    monkeypatch.setattr(cats, "FLASH", str(folder))
    today = dt.datetime.now(TZ).date()
    t0 = dt.datetime(today.year, today.month, today.day, 7, 5, tzinfo=TZ).timestamp()
    _write_flash(folder, today.isoformat(), [
        {"start": t0, "end": t0 + 90, "at": t0 + 10, "cats": ["Simba", "Nala"], "what": "moving"},
        {"start": t0 + 600, "end": t0 + 610, "cats": ["Simba"], "what": "eating"}], partial=True)
    _write_flash(folder, (today - dt.timedelta(days=1)).isoformat(), [{"start": t0 - 86400, "cats": ["Nala"]}])
    _write_flash(folder, (today - dt.timedelta(days=2)).isoformat(), [], video=False)      # no video: not listed
    return folder, today


def test_flash_reading(flash_dir):
    _, today = flash_dir
    days = cats.flash_days()
    assert days == [today.isoformat(), (today - dt.timedelta(days=1)).isoformat()]
    f = cats.flashes()[0]
    assert f["title"] == "Today so far" and f["near"] == "Today" and f["moments"] == 2
    assert f["cats"] == ["Nala", "Simba"] and f["per_cat"] == {"Nala": 1, "Simba": 2}
    first, second = f["items"]
    assert first["cats"] == ["Nala", "Simba"] and first["at_label"] == "07:05" and first["seek"] == 1.8
    assert second["seek"] == 5.8 and second["what_label"] == "Eating" and second["in_video"] == "0:06"
    assert cats.flashes()[1]["title"] == "Yesterday"
    assert cats.flash("../etc") is None and cats.flash("2001-01-01") is None


def test_weight_cards_and_table():
    day = 86400
    t = dt.datetime(2026, 10, 25, 14, 13, tzinfo=TZ).timestamp()
    data = {"Simba": [(t - 400 * day, 1.0), (t - 120 * day, 5.35), (t, 5.985)], "Nala": [(t - 30 * day, 3.9), (t, 3.95)]}
    nala, simba = cats.weight_cards(data, now=t + 16 * day)
    assert nala["cat"] == "Nala" and nala["slot"] == 1 and simba["slot"] == 2
    assert round(simba["d_prev"], 3) == 0.635 and simba["prev_words"].startswith("since ")
    assert simba["base_words"] == "in a year" and round(simba["d_base"], 3) == 4.985
    assert nala["base_words"].startswith("since ") and round(nala["age_days"]) == 16
    table = cats.weight_table(data)
    assert table[0]["Nala"] == 3.95 and table[0]["Simba"] == 5.985 and table[0]["label"] == "25 Oct 2026"


def test_weight_scale_not_from_zero():
    from app.site_cats import range_ticks
    ticks = range_ticks(3.78, 3.95)
    assert ticks[0] <= 3.78 and ticks[-1] >= 3.95 and ticks[0] > 3
    assert range_ticks(0.8, 3.94)[:2] == [0, 1]
    assert len(range_ticks(4.0, 4.0)) >= 2


def test_schedule_words():
    from app.site_cats import schedule_words
    noon = dt.datetime(2026, 11, 10, 12, 0, tzinfo=TZ).timestamp()
    words, nxt = schedule_words("20 0,19 * * *", now=noon)
    assert words == "Every day at 00:20 and 19:20"
    assert dt.datetime.fromtimestamp(nxt, TZ).strftime("%d %H:%M") == "10 19:20"
    late = dt.datetime(2026, 11, 10, 23, 0, tzinfo=TZ).timestamp()
    assert dt.datetime.fromtimestamp(schedule_words("20 0,19 * * *", now=late)[1], TZ).strftime("%d %H:%M") == "11 00:20"
    assert schedule_words("*/5 * * * *") == ("*/5 * * * *", None)


@pytest.fixture
def offline(monkeypatch, flash_dir):
    """Every source the pages read, without the network: Frigate, Kubernetes, the lab's own history."""
    from app import site_cats, store
    from app.sources import k8s
    now = time.time()
    t = dt.datetime(2026, 10, 25, 14, 13, tzinfo=TZ).timestamp()
    readings = {
        cats.weights: Reading(True, {"Simba": [(t - 120 * 86400, 5.35), (t, 5.985)], "Nala": [(t, 3.95)]}, "", now),
        cats.events: Reading(True, [{"id": "1794311566.471218-0yj3es", "start": now - 3600, "end": now - 3594,
                                     "zones": ["feeder_zone"], "where": "At the feeder", "length": 6, "score": .7,
                                     "clip": True, "snapshot": True, "sub_label": None}], "", now),
        cats.recordings: Reading(True, {dt.datetime.now(TZ).date().isoformat(): {7: {"events": 2}}}, "", now),
        cats.frigate: Reading(True, {"version": "0.16.3"}, "", now),
        cats.monitor: Reading(True, [{"block": "b", "start": now - 40000, "end": now - 36000, "window": 4000,
                                      "presence": {"Simba": 3900}, "suspicious": ["Simba"], "middle": now - 38000}], "", now),
        k8s.jobs: Reading(True, {"cronjobs": {
            "cat-flash": {"name": "cat-flash", "schedule": "20 0,19 * * *", "suspended": False, "last_success": now - 9000},
            "cat-monitor": {"name": "cat-monitor", "schedule": "16 3 * * *", "suspended": True, "last_success": now - 40000}},
            "jobs": [{"name": "cat-monitor-1", "cronjob": "cat-monitor", "state": "ok", "start": now - 40000,
                      "end": now - 36000}]}, "", now),
    }
    for src, reading in readings.items():
        monkeypatch.setattr(src, "get", lambda *a, _r=reading: _r)
    monkeypatch.setattr(store, "job_history", lambda *a, **k: [])
    from starlette.testclient import TestClient
    return TestClient(site_cats.app)


def test_pages(offline, flash_dir):
    _, today = flash_dir
    pages = {"/": ["Simba and Nala", "Today so far", "Right now", "data-live", "5.99"],
             "/flashes": ["Flash of the day", "Today so far", "Yesterday"],
             f"/flashes/{today.isoformat()}": ["Moments", "07:05", 'data-seek="1.8"', "Nala and Simba"],
             "/camera": ["Look back", "Hour by hour", "At the feeder", "An hour ago"],
             "/weight": ["Add a weigh-in", "Weigh-ins", "+0.64 kg"],
             "/status": ["Feeding monitor", "Paused", "Every day at 00:20 and 19:20", "in view all along", "Online"]}
    for path, words in pages.items():
        r = offline.get(path)
        assert r.status_code == 200, path
        for w in words:
            assert w in r.text, (path, w)
    gone = offline.get("/flashes/2001-01-01")
    assert gone.status_code == 404 and "Nothing here" in gone.text and "No flash for that day" in gone.text
    assert offline.get("/nowhere").status_code == 404
    assert offline.get("/media/event/bad id/clip.mp4").status_code == 404 and "Nothing here" not in         offline.get("/media/event/bad/clip.mp4").text                   # media keep their plain answer
    old = offline.get(f"/?day={today.isoformat()}", follow_redirects=False)        # links from the one-page site
    assert old.status_code == 302 and old.headers["location"].endswith(f"/flashes/{today.isoformat()}")
    assert offline.get("/camera?at=2001-01-01T10:00").status_code == 200       # out of range: told, not an error


def test_tile(offline):
    from app import summary
    out = summary.catcam()
    assert out["flash"].endswith("2 moments, 10 s") and out["monitor"] == "paused" and out["weighed"].endswith("days ago")
