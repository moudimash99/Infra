"""Alarms (lab.machaka.net/machines/alarms): the bell's full list. Every Prometheus alert rule (alert-rules.yml):
firing now, about to fire (pending) or quiet; what it watches in plain words; when it fired in the last 7 days (from
Prometheus's own ALERTS series); and what is muted in Alertmanager. The same rules email the owner, so this page
and the inbox always agree.
"""
import concurrent.futures
import datetime as dt
import re
import time

from . import prom, source
from ..config import ALERTMANAGER, PROMETHEUS, TZ
from .prom import _http

STEP = 300
# A rule's summary is written for one alert ("acer-laptop: memory 93% used"); for the rule itself, its placeholders
# become words.
LABEL_WORDS = {"instance": "a machine", "node": "a machine", "pod": "an app", "job_name": "a job", "cronjob": "a job",
               "deployment": "an app", "daemonset": "a per-machine app", "camera_name": "", "mountpoint": "",
               "container": "a container", "namespace": "", "storage": "", "job": "a target", "reason": "a reason"}
AREAS = {"nodes": "Machines", "machines": "Machines", "gpu": "GPU", "kubernetes": "Kubernetes", "jobs": "Jobs",
         "scheduled-jobs": "Jobs", "apps": "Apps", "alerting": "Alerts", "monitoring": "Alerts"}
# Where a rule's own page is: what it watches (an alert firing now links to the very machine or app instead).
AREA_PAGES = {"Machines": "/server", "GPU": "/acer#gpu", "Kubernetes": "/apps", "Jobs": "/jobs", "Apps": "/apps",
              "Alerts": "/alarms#rules"}
# Rules whose summary is mostly its numbers: a plain title instead (the limits are alert-rules.yml's).
TITLES = {
    "NodeHot": "A machine is running hot (over 90 °C)", "NodeVeryHot": "A machine is very hot (over 97 °C)",
    "NodeMemoryHigh": "A machine is short of memory", "NodeMemoryCritical": "A machine is almost out of memory",
    "NodeCPUBusy": "A machine's CPU has been busy for 30 minutes", "NodeLoadHigh": "A machine is overloaded",
    "NodeDiskSpaceLow": "A disk is getting full", "NodeDiskSpaceCritical": "A disk is nearly full",
    "NodeDiskFillingUp": "A disk will be full within a day", "NodeFilesystemReadOnly": "A disk went read-only",
    "DeploymentUnavailable": "An app has copies down", "DaemonSetUnavailable": "A per-machine app is missing somewhere",
    "ContainerMemoryNearLimit": "An app is close to its memory limit", "ContainerCannotStart": "An app can't start",
    "JobRunningTooLong": "A job is still running long after it should have finished",
    "GPUHot": "The GPU is running hot (over 85 °C)", "GPUVeryHot": "The GPU is very hot",
    "GPUMemoryHigh": "The GPU's memory is nearly full", "TargetDown": "Monitoring can't read one of its sources",
}


def rule_title(summary):
    """'{{ $labels.instance }}: memory {{ $value | humanizePercentage }} used' -> 'A machine: memory used'."""
    s = re.sub(r"\(\s*\{\{[^}]*\$value[^}]*\}\}[^)]*\)", "", summary or "")       # "(93% used)" style asides
    s = re.sub(r"\{\{\s*\$value[^}]*\}\}\s*", "", s)
    s = re.sub(r"\{\{\s*\$labels\.(\w+)\s*\}\}", lambda m: LABEL_WORDS.get(m.group(1), m.group(1)), s)
    s = re.sub(r"\s+", " ", s).replace(" :", ":").replace("disk  ", "disk ").strip(" ,:")
    s = re.sub(r"\bdisk (has|nearly|will|went)", r"a disk \1", s)
    return s[:1].upper() + s[1:]


def first_sentence(text):
    text = re.sub(r"\{\{[^}]*\}\}", "…", text or "").strip()
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]


def fetch():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rules = pool.submit(lambda: _http.get(f"{PROMETHEUS}/api/v1/rules", params={"type": "alert"}).json())
        history = pool.submit(prom.query_range, 'max by (alertname, severity) (ALERTS{alertstate="firing"})', 168, STEP)
        silences = pool.submit(lambda: _http.get(f"{ALERTMANAGER}/api/v2/silences").json())
        out = {"rules": rules.result()["data"]["groups"], "at": time.time()}
        for key, fut in (("history", history), ("silences", silences)):
            try:
                out[key] = fut.result()
            except Exception:                  # noqa: BLE001 - the page still lists the rules
                out[key] = None
    return out


def day_starts(now, days=7):
    today = dt.datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return [(today - dt.timedelta(days=days - 1 - i)).timestamp() for i in range(days)]


def episodes(points, step=STEP):
    """[(start, end)] of each firing: points closer than 1.5 steps are one firing."""
    out = []
    for t, v in points:
        if not v:
            continue
        if out and t - out[-1][1] <= 1.5 * step:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [tuple(e) for e in out]


def build(raw, now=None):
    now = now or raw.get("at") or time.time()
    starts = day_starts(now)
    fired = {}
    for labels, points in raw.get("history") or []:
        fired.setdefault(labels.get("alertname", "?"), []).extend(episodes(points))
    rules = []
    for g in raw.get("rules") or []:
        area = AREAS.get(g.get("name", ""), g.get("name", "").capitalize())
        for r in g.get("rules", []):
            if r.get("type") != "alerting":
                continue
            name = r["name"]
            sev = (r.get("labels") or {}).get("severity", "warning")
            eps = sorted(fired.get(name, []))
            cells = []
            for i, d0 in enumerate(starts):
                d1 = starts[i + 1] if i + 1 < len(starts) else now + 1
                mine = [e for e in eps if e[0] < d1 and e[1] >= d0]
                day = dt.datetime.fromtimestamp(d0, TZ)
                if mine:
                    times = ", ".join(dt.datetime.fromtimestamp(max(e[0], d0), TZ).strftime("%H:%M") for e in mine[:4])
                    tip = f"{day:%a} {day.day} {day:%b}: fired {len(mine)}× ({times}{'…' if len(mine) > 4 else ''})"
                    cells.append({"state": "failed" if sev == "critical" else "some", "tip": tip})
                else:
                    cells.append({"state": None, "tip": f"{day:%a} {day.day} {day:%b}: quiet"})
            state = r.get("state", "inactive")
            alerts = r.get("alerts") or []
            ann = r.get("annotations") or {}
            row = {
                "name": name, "area": area, "severity": sev, "href": AREA_PAGES.get(area, "/alarms#rules"), "title": TITLES.get(name) or rule_title(ann.get("summary", name)),
                "what": first_sentence(ann.get("description", "")), "wait": r.get("duration") or 0,
                "event": (r.get("labels") or {}).get("kind") == "event", "rule_state": state,
                "firing": [a for a in alerts if a.get("state") == "firing"],
                "pending": [a for a in alerts if a.get("state") == "pending"],
                "week": len(eps), "last": eps[-1][1] if eps else None, "cells": cells,
                "health": r.get("health", "ok"), "error": r.get("lastError", ""),
            }
            row["state"] = (("bad" if sev == "critical" else "warn") if row["firing"] else
                            "info" if row["pending"] else "bad" if row["health"] != "ok" else "ok")
            rows_text = []
            if row["firing"]:
                rows_text.append(f"firing now ({len(row['firing'])})" if len(row["firing"]) > 1 else "firing now")
            elif row["pending"]:
                rows_text.append("about to fire")
            rows_text.append(f"fired {row['week']}× this week" if row["week"] else "quiet all week")
            if row["health"] != "ok":
                rows_text.append(f"the rule fails: {row['error'][:120]}")
            row["text"] = " · ".join(rows_text)
            rules.append(row)
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    rules.sort(key=lambda r: (order[r["state"]], -r["week"], r["area"], r["name"]))
    silences = []
    for s in raw.get("silences") or []:
        if (s.get("status") or {}).get("state") != "active":
            continue
        silences.append({"what": ", ".join(f"{m['name']}{'=' if m.get('isEqual', True) else '!='}{m['value']}"
                                           for m in s.get("matchers", [])),
                         "comment": s.get("comment", ""), "by": s.get("createdBy", ""), "ends": _ts(s.get("endsAt"))})
    return {"rules": rules, "silences": silences, "starts": starts, "history_ok": raw.get("history") is not None,
            "silences_ok": raw.get("silences") is not None, "at": now,
            "areas": sorted({r["area"] for r in rules})}


def _ts(iso):
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


@source("alarms", ttl=60)
def read():
    return fetch()
