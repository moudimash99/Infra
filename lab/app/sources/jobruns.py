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


# When each kill for memory happened: the first sample a container shows OOMKilled as its (last) stop reason.
OOM_WHEN = ('max by (pod) ((kube_pod_container_status_terminated_reason{namespace="apps",reason="OOMKilled"} or '
            'kube_pod_container_status_last_terminated_reason{namespace="apps",reason="OOMKilled"}) == 1)')
# A CronJob run's pod: <cronjob>-<8 digits>-<5>.
JOB_POD = 'pod=~".+-[0-9]{8}-[a-z0-9]{5}"'
MEM_RANGES = {"24h": (24, 300, "5m"), "7d": (168, 1800, "30m")}


def fetch():
    raw = {key: query(expr) for key, expr in Q7.items()}
    raw["pc"] = query_range("pc_task_ok", DAYS * 24, 1800)
    try:
        raw["oom_when"] = query_range(OOM_WHEN, DAYS * 24, 300)
    except Exception:                      # noqa: BLE001 - the kills are still listed, without their time
        raw["oom_when"] = []
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
        paused = is_paused(m.get("note"))
        fake = [{"start": t, "result": "ok" if v >= 1 else "failed", "took": None} for t, v in values]
        days = _cells(fake, starts)
        for c in days:                   # a report every 30 min is not a run: say "reported fine / failed"
            c["runs"] = 0
            when = dt.datetime.fromtimestamp(c["day"], TZ)
            if paused and c["state"] == "failed":       # stopped on purpose: no colour, nothing is going wrong
                c["state"] = "paused"
            words = {"failed": "it failed", "ok": "fine", None: "no report", "paused": f"paused ({m.get('note', '')})"}
            c["tip"] = f"{when:%a} {when.day}: " + words[c["state"]]
        pc_cells.setdefault(task, days)

    job_of_pod = {p: j for j, ps in pods_of.items() for p in ps}
    first_seen = {}
    for m, values in raw.get("oom_when", []):
        if values:
            first_seen[m.get("pod")] = values[0][0]
    run_of = {r["job"]: r for rs in runs.values() for r in rs}
    ooms = []
    for p in oom_pods:                    # every kill: when, which job or app, its limit and how high it got
        job = job_of_pod.get(p)
        run = run_of.get(job) if job else None
        at = first_seen.get(p) or ((run["end"] or run["start"]) if run else None)
        ooms.append({"name": owner.get(job) if job else app_name(p), "kind": "job" if job else "app", "pod": p,
                     "at": at, "limit": limit.get(p), "peak": peak.get(p),
                     "after": (at - run["start"]) if run and at and at >= run["start"] else None})
    ooms.sort(key=lambda o: -(o["at"] or 0))
    return {"runs": runs, "cells": cells, "pc_cells": pc_cells, "ooms": ooms, "starts": starts, "at": now,
            "fails": {cj: failures(rs, now) for cj, rs in runs.items()}}


def is_paused(note):
    """The PC reports a task it stopped on purpose (Upwork asked for a login) with a note starting "stopped"."""
    return str(note or "").strip().lower().startswith(("stopped", "paused"))


def failures(runs, now):
    """How often a job fails, and whether the last day is normal for it: {text, unusual, rate}."""
    done = [r for r in runs if r["result"] != "running"]
    bad = lambda rs: sum(1 for r in rs if r["result"] in ("failed", "oom"))
    n7, f7 = len(done), bad(done)
    day = [r for r in done if r["start"] >= now - 86400]
    before = [r for r in done if r["start"] < now - 86400]
    n1, f1, n0, f0 = len(day), bad(day), len(before), bad(before)
    if not n7:
        return {"text": "no run this week", "unusual": False, "rate": None}
    if not f7:
        return {"text": f"{n7} run{'s' if n7 != 1 else ''} this week, none failed", "unusual": False, "rate": 0.0}
    text = f"{f7} of {n7} runs failed this week ({f7 / n7:.0%})" if n7 >= 5 else f"{f7} of {n7} runs failed this week"
    unusual = False
    if n1 and n0 >= 5:
        usual = f0 / n0
        if f1 and f1 / n1 > max(2 * usual, usual + 0.1):
            unusual = True
            text += f": more than usual, {f1} of {n1} in the last 24 h (usually {usual:.0%})"
        elif f1:
            text += f": normal for it (usually {usual:.0%})"
        else:
            text += ", none in the last 24 h"
    else:
        last = max((r["start"] for r in done if r["result"] in ("failed", "oom")), default=None)
        if last:
            text += f", the last one {dt.datetime.fromtimestamp(last, TZ):%a %d %b %H:%M}"
    return {"text": text, "unusual": unusual, "rate": f7 / n7}


@source("jobruns.mem", ttl=300)
def memory(range_key):
    """Each CronJob's real memory over time: the biggest of its runs at each point (two runs overlap for a moment
    when one starts as the last ends; each has its own limit), peaks within each point's window."""
    hours, step, w = MEM_RANGES[range_key]
    expr = ('sum by (pod) (max_over_time(container_memory_working_set_bytes{job="kubelet-cadvisor",namespace="apps",'
            f'container!="",container!="POD",{JOB_POD}}}[{w}]))')
    out = {}
    for labels, points in query_range(expr, hours, step):
        acc = out.setdefault(app_name(labels.get("pod", "")), {})
        for t, v in points:
            acc[t] = max(acc.get(t, 0), v)
    return {cj: sorted(acc.items()) for cj, acc in out.items()}


def per_job(data, mem, hours):
    """One small card per CronJob: how long each run took (minutes), its memory over time (GB) against its limit, and
    the numbers under them. Every job, not only the biggest five."""
    G = 1024 ** 3
    since, now = data["at"] - hours * 3600, data["at"]
    out = {}
    for cj in sorted(set(data["runs"]) | set(mem or {})):
        rs = [r for r in data["runs"].get(cj, []) if r["start"] >= since]
        took = [(r["start"], r["took"] / 60) for r in rs if r["took"] is not None and r["result"] != "running"]
        if len(took) > 150:              # the archiver runs ~500 times a day: the slowest run per slice
            took = _peaks(took, hours * 3600 / 150)
        used = [(t, v / G) for t, v in (mem or {}).get(cj, []) if t >= since]
        limits = [r["limit"] for r in rs if r["limit"]] or [r["limit"] for r in data["runs"].get(cj, []) if r["limit"]]
        lim = limits[-1] / G if limits else None
        mem_series = [{"name": "Memory", "slot": 1, "points": used}] if used else []
        if mem_series and lim:
            mem_series.append({"name": "Limit", "slot": 8, "points": [(since, lim), (now, lim)]})
        peak = max((r["mem"] for r in rs if r["mem"]), default=None)
        times = sorted(r["took"] for r in rs if r["took"] is not None and r["result"] != "running")
        out[cj] = {"took": [{"name": "Minutes", "slot": 2, "points": took}] if took else [], "mem": mem_series,
                   "span": (since, now),
                   "runs": len(rs), "median": times[len(times) // 2] if times else None,
                   "longest": times[-1] if times else None, "peak": peak, "limit": lim * G if lim else None,
                   "ooms": sum(1 for r in rs if r["result"] == "oom")}
    return out


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
