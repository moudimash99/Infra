"""Every scheduled job's last 7 days, for lab.machaka.net/machines/jobs (Grafana's "Scheduled jobs" and "PC jobs").

Kubernetes keeps only the last few Jobs of a CronJob, but Prometheus remembers every run's series for its retention,
so each run comes from max_over_time over 7 days (the queries of k8s/monitoring/dashboards/make.py): its CronJob, start,
end, whether it failed, its pods (machine, peak memory, memory limit) and whether one was killed for memory.

Per job: one cell per day for the last 7 days (the worst result that day, how many runs, how long they took), and per
run the points for the graphs "how long each run took" and "memory against the limit". The PC's tasks get the same
day cells from pc_task_ok (0 when a run failed), which the PC reports every 10 minutes while it's on.
"""
import datetime as dt
import time

from . import source
from ..config import TZ
from .memory import app_name
from .prom import query, query_range

OOM7 = ('(max by (namespace, pod, container) (max_over_time(kube_pod_container_status_terminated_reason'
        '{namespace="apps",reason="OOMKilled"}[7d])) or max by (namespace, pod, container) (max_over_time('
        'kube_pod_container_status_last_terminated_reason{namespace="apps",reason="OOMKilled"}[7d]))) > 0')
Q7 = {
    "owner": 'max by (job_name, owner_name) (max_over_time(kube_job_owner{namespace="apps",owner_kind="CronJob"}[7d]))',
    "start": 'max by (job_name) (max_over_time(kube_job_status_start_time{namespace="apps"}[7d]))',
    "end": 'max by (job_name) (max_over_time(kube_job_status_completion_time{namespace="apps"}[7d]))',
    "failed": 'max by (job_name) (max_over_time(kube_job_failed{namespace="apps",condition="true"}[7d])) == 1',
    "active": 'kube_job_status_active{namespace="apps"} > 0',
    "pod_job": ('max by (pod, created_by_name, node) (max_over_time(kube_pod_info{namespace="apps",'
                'created_by_kind="Job"}[7d]))'),
    "mem_peak": ('max by (pod) (max_over_time(container_memory_working_set_bytes{job="kubelet-cadvisor",'
                 'namespace="apps",container!="",container!="POD"}[7d]))'),
    "mem_limit": 'max by (pod) (max_over_time(kube_pod_container_resource_limits{namespace="apps",resource="memory"}[7d]))',
    "oom": OOM7,
}
DAYS = 7
WORST = {"oom": 5, "failed": 4, "some": 3, "ok": 2, "running": 1, None: 0}   # a day's colour: its worst run


def fetch():
    raw = {key: query(expr) for key, expr in Q7.items()}
    raw["pc"] = query_range("pc_task_ok", DAYS * 24, 1800)
    raw["at"] = time.time()
    return raw


def day_starts(now, days=DAYS):
    """Local midnights, oldest first: the last `days` days, today included."""
    today = dt.datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return [(today - dt.timedelta(days=i)).timestamp() for i in range(days - 1, -1, -1)]


def _day_of(ts, starts):
    for i in range(len(starts) - 1, -1, -1):
        if ts >= starts[i]:
            return i
    return None


def _cells(runs, starts):
    cells = [{"day": s, "state": None, "runs": 0, "failed": 0, "oom": 0, "took": []} for s in starts]
    for r in runs:
        i = _day_of(r["start"], starts)
        if i is None:
            continue
        c = cells[i]
        c["runs"] += 1
        c["failed"] += r["result"] == "failed"
        c["oom"] += r["result"] == "oom"
        if r["took"] is not None:
            c["took"].append(r["took"])
        if WORST[r["result"]] > WORST[c["state"]]:
            c["state"] = r["result"]
    for c in cells:
        bad = c["failed"] + c["oom"]
        if bad and c["runs"] >= 5 and bad / c["runs"] < 0.2:     # the archiver runs ~300 times a day: a few failed
            c["state"] = "some"                                    # runs make the day amber, not red
        when = dt.datetime.fromtimestamp(c["day"], TZ)
        took = f", {_minutes(sum(c['took']) / len(c['took']))} each" if c["took"] else ""
        bad = []
        if c["failed"]:
            bad.append(f"{c['failed']} failed")
        if c["oom"]:
            bad.append(f"{c['oom']} killed for memory")
        c["tip"] = (f"{when:%a} {when.day}: " + (f"{c['runs']} run{'s' if c['runs'] > 1 else ''}"
                    + (" (" + ", ".join(bad) + ")" if bad else ", all fine" if c["state"] == "ok" else "")
                    + took if c["runs"] else "no run"))
    return cells


def _minutes(seconds):
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def build(raw, now=None):
    """{"runs": {cronjob: [run, ...] oldest first}, "cells": {cronjob: [7 day cells]}, "pc_cells": {task: [...]},
    "ooms": [(cronjob or pod, limit)], "starts": [local midnights]}."""
    now = now or raw.get("at") or time.time()
    starts = day_starts(now)
    owner = {m.get("job_name"): m.get("owner_name") for m, _ in raw.get("owner", [])}
    start = {m.get("job_name"): v for m, v in raw.get("start", [])}
    end = {m.get("job_name"): v for m, v in raw.get("end", [])}
    failed = {m.get("job_name") for m, _ in raw.get("failed", [])}
    active = {m.get("job_name") for m, _ in raw.get("active", [])}
    pods_of, node_of = {}, {}
    for m, _ in raw.get("pod_job", []):
        job, pod = m.get("created_by_name"), m.get("pod")
        pods_of.setdefault(job, set()).add(pod)
        if m.get("node"):
            node_of[job] = m["node"]
    peak = {m.get("pod"): v for m, v in raw.get("mem_peak", [])}
    limit = {m.get("pod"): v for m, v in raw.get("mem_limit", [])}
    oom_pods = {m.get("pod") for m, _ in raw.get("oom", [])}

    runs = {}
    for job, cj in owner.items():
        if job not in start:
            continue
        pods = pods_of.get(job, set())
        if pods & oom_pods:
            result = "oom"
        elif job in failed:
            result = "failed"
        elif job in end:
            result = "ok"
        elif job in active:
            result = "running"
        else:
            continue                      # deleted before it finished, or not started: nothing to say
        mem = max((peak[p] for p in pods if p in peak), default=None)
        lim = max((limit[p] for p in pods if p in limit), default=None)
        took = (end[job] - start[job]) if job in end else (now - start[job]) if result == "running" else None
        runs.setdefault(cj, []).append({"job": job, "start": start[job], "end": end.get(job), "took": took,
                                        "result": result, "node": node_of.get(job, ""), "mem": mem, "limit": lim})
    for cj in runs:
        runs[cj].sort(key=lambda r: r["start"])
    cells = {cj: _cells(rs, starts) for cj, rs in runs.items()}

    pc_cells = {}
    for m, values in raw.get("pc", []):
        task = (m.get("task") or "?").lstrip("\\")
        fake = [{"start": t, "result": "ok" if v >= 1 else "failed", "took": None} for t, v in values]
        days = _cells(fake, starts)
        for c in days:                   # a report every 30 min is not a run: say "reported fine / failed"
            c["runs"] = 0
            when = dt.datetime.fromtimestamp(c["day"], TZ)
            c["tip"] = f"{when:%a} {when.day}: " + {"failed": "it failed", "ok": "fine", None: "no report"}[c["state"]]
        pc_cells[task] = days

    job_of_pod = {p: j for j, ps in pods_of.items() for p in ps}
    ooms = {}
    for p in oom_pods:                    # jobs by their CronJob, apps by their name; the biggest limit seen
        job = job_of_pod.get(p)
        name = owner.get(job) if job else app_name(p)
        kind = "job" if job else "app"
        lim = max(limit.get(p) or 0, (ooms.get(name) or {}).get("limit") or 0) or None
        ooms[name] = {"name": name, "kind": kind, "limit": lim}
    ooms = sorted(ooms.values(), key=lambda o: o["name"])
    return {"runs": runs, "cells": cells, "pc_cells": pc_cells, "ooms": ooms, "starts": starts, "at": now}


def _peaks(points, bucket):
    """The highest value per `bucket` seconds, at the bucket's middle: the archiver's ~300 runs a day become one point
    an hour instead of a solid band."""
    best = {}
    for x, y in points:
        k = int(x // bucket)
        if k not in best or y > best[k]:
            best[k] = y
    return [((k + 0.5) * bucket, y) for k, y in sorted(best.items())]


def graphs(data, hours, most=5):
    """The two graphs for the last `hours`: how long each run took (minutes) and its peak memory as a share of its
    limit, one series per job (the `most` slowest or biggest; the charts have five colours), runs grouped into
    hourly peaks over a day and 6-hour peaks over a week."""
    since = data["at"] - hours * 3600
    bucket = 3600 if hours <= 48 else 6 * 3600
    took, mem = [], []
    for cj, rs in data["runs"].items():
        mine = [r for r in rs if r["start"] >= since]
        t = _peaks([(r["start"], r["took"] / 60) for r in mine if r["took"] is not None and r["result"] != "running"],
                   bucket)
        m = _peaks([(r["start"], 100 * r["mem"] / r["limit"]) for r in mine if r["mem"] and r["limit"]], bucket)
        if t:
            took.append({"name": cj, "points": t, "top": max(y for _, y in t)})
        if m:
            mem.append({"name": cj, "points": m, "top": max(y for _, y in m)})
    out = {}
    for key, series in (("took", took), ("mem", mem)):
        series = sorted(series, key=lambda s: -s["top"])[:most]
        out[key] = [{"name": s["name"], "slot": i + 1, "points": s["points"]} for i, s in enumerate(series)]
    return out


@source("jobruns", ttl=300)
def week():
    return build(fetch())
