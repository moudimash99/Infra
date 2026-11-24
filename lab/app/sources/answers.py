"""The questions the owner asked while the Grafana dashboards were being built, answered on one page
(lab.machaka.net/machines/):

  Why is memory high on a machine, and who is using it, in containers and out?   every app stacked over time, up to
        what the machine really uses, with its total and the room for pods; the lowest point and who had grown then
  Was anything killed or pushed out, by whom, and why?     its own limit, the machine running out (the kernel picked
        it), or pushed out for a higher rank
  Does every app get in, or wait for room, and for how long?     each app's running / waiting / failed over time, per
        machine, with its rank; jobs from their real runs
  What rank does each app have, what does it book, what does it really use?     per machine
  Too hot? Who is on the GPU?

All series of one range share a time grid (one end, one step), so stacks add up and the hover lines up across charts.
"""
import concurrent.futures
import re
import time

from . import cron, prom, source
from ..config import PROMETHEUS
from .memory import DOCKER_NODE, DOCKER_WS, G, K3S_CGROUP, MACHINE_NAMES, WS, app_name
from .prom import _http

RANGES = {"24h": (24, 300), "7d": (168, 1800)}
NODES = list(MACHINE_NAMES)
CONT = 'container!="",container!="POD"'
GPU_POD = ('sum by (pod) (gpu_process_sm_percent * on (pod_uid) group_left (pod) '
           'label_replace(kube_pod_info, "pod_uid", "$1", "uid", "(.+)"))')
SMALL = 0.02          # an app whose peak stays under 2% of the machine joins "smaller apps"
MOST_APPS, MOST_DOCKER = 12, 3   # colours a reader can tell apart; the rest are added up in one band each


def queries():
    q = {}
    for n in NODES:
        q[f"apps:{n}"] = f'sum by (namespace, pod) ({WS},node="{n}",{CONT}}})'
        q[f"k3s:{n}"] = f'max({WS},node="{n}",id=~"{K3S_CGROUP}"}})'
        q[f"used:{n}"] = f'node_memory_MemTotal_bytes{{instance="{n}"}} - node_memory_MemAvailable_bytes{{instance="{n}"}}'
        q[f"total:{n}"] = f'node_memory_MemTotal_bytes{{instance="{n}"}}'
        q[f"room:{n}"] = f'kube_node_status_allocatable{{resource="memory",node="{n}"}}'
        q[f"temp:{n}"] = f'max(node_hwmon_temp_celsius{{instance="{n}"}})'
    q["docker"] = f"max by (name) ({DOCKER_WS})"
    q["running"] = 'max by (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)'
    q["waiting"] = "max by (namespace, pod) (kube_pod_status_unschedulable == 1)"
    q["failed"] = 'max by (namespace, pod) (kube_pod_status_phase{phase="Failed"} == 1)'
    q["where"] = "max by (namespace, pod, node) (kube_pod_info)"
    q["pushed"] = 'max by (namespace, pod, reason) (kube_pod_status_reason{reason=~"PreemptionByScheduler|Evicted"} == 1)'
    q["oom"] = ('max by (namespace, pod) ((kube_pod_container_status_terminated_reason{reason="OOMKilled"} or '
                'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}) == 1)')
    q["kernel"] = "increase(node_vmstat_oom_kill[10m]) > 0"
    q["gpu_temp"] = "nvidia_smi_temperature_gpu"
    q["gpu_pods"] = GPU_POD
    return q


def fetch(range_key):
    hours, step = RANGES[range_key]
    end = int(time.time() // step * step)
    start = end - hours * 3600

    def ask(expr):
        r = _http.get(f"{PROMETHEUS}/api/v1/query_range", params={"query": expr, "start": start, "end": end, "step": step})
        r.raise_for_status()
        return [(s["metric"], [(float(t), float(v)) for t, v in s["values"]]) for s in r.json()["data"]["result"]]

    raw, errors = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {k: pool.submit(ask, e) for k, e in queries().items()}
        for k, f in futs.items():
            try:
                raw[k] = f.result()
            except Exception as e:          # noqa: BLE001 - one gap, not a broken page
                raw[k] = []
                errors.append(e)
    if len(errors) > len(futs) // 2:
        raise errors[0]
    for key, expr in (("cj_info", "kube_cronjob_info"), ("cj_suspend", "kube_cronjob_spec_suspend == 1")):
        try:
            raw[key] = prom.query(expr)
        except Exception:                      # noqa: BLE001 - the jobs are listed without their schedule
            raw[key] = []
    raw.update(start=start, end=end, step=step)
    return raw


# ---- from numbers to answers (tests feed `raw` directly) ----------------------------------------------------------

def grid(raw):
    return [raw["start"] + i * raw["step"] for i in range((raw["end"] - raw["start"]) // raw["step"] + 1)]


def on_grid(points, X, scale=1.0):
    at = {int(t): v for t, v in points}
    return [round(at[t] * scale, 4) if t in at else None for t in X]


def one(raw, key, X, scale=1.0):
    rows = raw.get(key) or []
    return on_grid(rows[0][1], X, scale) if rows else [None] * len(X)


def memory_stack(raw, node, X, href):
    """Every app on the machine stacked (biggest at the bottom), then k3s, the Docker containers, and what the machine
    uses outside them all, so the top of the stack is the memory really in use."""
    total = one(raw, f"total:{node}", X, 1 / G)
    used = one(raw, f"used:{node}", X, 1 / G)
    size = max((v for v in total if v), default=8)
    apps = {}
    for labels, pts in raw.get(f"apps:{node}", []):
        key = (labels.get("namespace", ""), app_name(labels.get("pod", "")))
        acc = apps.setdefault(key, {})
        for t, v in pts:
            acc[int(t)] = acc.get(int(t), 0) + v / G
    series, small = [], [0.0] * len(X)
    for (ns, name), acc in apps.items():
        vals = [round(acc[t], 4) if t in acc else None for t in X]
        if max((v or 0) for v in vals) < SMALL * size:
            small = [a + (v or 0) for a, v in zip(small, vals)]
            continue
        series.append({"name": name, "values": vals, "href": href(ns, name)})
    k3s = one(raw, f"k3s:{node}", X, 1 / G)
    if any(k3s):
        series.append({"name": "k3s itself", "values": k3s})
    area = lambda s: sum(v or 0 for v in s["values"])
    series.sort(key=lambda s: -area(s))
    n_small = sum(1 for (_, _), acc in apps.items() if max(acc.values(), default=0) < SMALL * size)
    for s in series[MOST_APPS:]:
        small = [a + (v or 0) for a, v in zip(small, s["values"])]
    n_small += max(0, len(series) - MOST_APPS)
    series = series[:MOST_APPS]
    if node == DOCKER_NODE:                      # the server's Docker containers: the biggest by name, the rest added up
        docker = sorted(({"name": f"{labels.get('name', '?')} (Docker)", "values": on_grid(pts, X, 1 / G)}
                         for labels, pts in raw.get("docker", [])), key=lambda s: -area(s))
        series += [d for d in docker[:MOST_DOCKER] if max((v or 0) for v in d["values"]) >= SMALL * size]
        rest = [d for d in docker if d not in series]
        if rest:
            series.append({"name": f"other Docker containers ({len(rest)})",
                           "values": [round(sum(d["values"][i] or 0 for d in rest), 4) for i in range(len(X))]})
        series.sort(key=lambda s: -area(s))
    if any(small):
        series.append({"name": f"smaller apps ({n_small})", "values": [round(v, 4) for v in small]})
    outside = []
    for i, u in enumerate(used):
        inside = sum(s["values"][i] or 0 for s in series)
        outside.append(round(max(u - inside, 0), 4) if u is not None else None)
    series.append({"name": "outside them: system, programs, kernel", "values": outside, "color": "#9aa0a6"})
    return {"kind": "area", "stacked": True, "unit": " GB", "x": X, "series": series,
            "lines": [{"name": "the machine's memory", "values": total, "dash": True, "color": "var(--bad)"},
                      {"name": "room for pods (bookings)", "values": one(raw, f"room:{node}", X, 1 / G), "dash": True,
                       "color": "var(--text-3)"}]}


def memory_answer(chart, X):
    """Free now, the lowest point and who had grown then (against their own usual, the median)."""
    total, used = chart["lines"][0]["values"], None
    used = [sum((s["values"][i] or 0) for s in chart["series"]) for i in range(len(X))]
    free = [(t - u) if t is not None else None for t, u in zip(total, used)]
    idx = [i for i, f in enumerate(free) if f is not None and used[i] > 0]
    if not idx:
        return None
    now_i, low_i = idx[-1], min(idx, key=lambda i: free[i])
    def median(vals):
        v = sorted(x for x in vals if x is not None)
        return v[len(v) // 2] if v else 0
    grew = []
    for s in chart["series"]:
        v = s["values"][low_i]
        if v is None or s["name"].startswith(("outside", "smaller")):
            continue
        usual = median(s["values"])
        if v - usual > 0.1:
            grew.append((v - usual, s["name"], v, usual))
    grew.sort(reverse=True)
    top_now = sorted(((s["values"][now_i] or 0, s["name"]) for s in chart["series"]
                      if not s["name"].startswith(("outside", "smaller"))), reverse=True)[:3]
    return {"free": free[now_i], "total": total[now_i], "used": used[now_i], "top": top_now,
            "low": free[low_i], "low_at": X[low_i], "grew": grew[:3],
            "outside": chart["series"][-1]["values"][now_i] or 0}


def segments(points_by_key, step, until):
    """{key: [t...]} -> {key: [[t0, t1]]}: consecutive samples (gap under 1.5 steps) are one stretch."""
    out = {}
    for key, ts in points_by_key.items():
        segs = []
        for t in sorted(ts):
            if segs and t - segs[-1][1] <= 1.5 * step:
                segs[-1][1] = t
            else:
                segs.append([t, t])
        out[key] = [[a, min(b + step, until)] for a, b in segs]
    return out


SYSTEM_GROUPS = {"Monitoring", "Platform"}
SYSTEM_NAMESPACES = {"kube-system", "flux-system", "monitoring"}


def is_system(P, ns, app):
    """Plumbing the owner doesn't need day to day (k3s's parts, Flux, monitoring agents, backups): hidden until asked
    for, or until something goes wrong with it."""
    return ns in SYSTEM_NAMESPACES or (P.group(ns, app) if P else "") in SYSTEM_GROUPS


def tables(raw, week, rows_by_app, href, P=None, sched=None):
    """Per machine, one row per app: its rank, what it books and really uses, and its bar over time (running /
    waiting for room / failed), with marks where it was pushed out or killed for memory and, for a job, ticks at the
    times it was scheduled. A CronJob's bars are its real runs (start to end) from jobruns, not 5-minute samples."""
    step, until, start = raw["step"], raw["end"], raw["start"]
    where = {(m.get("namespace"), m.get("pod")): m.get("node") or "" for m, _ in raw.get("where", [])}
    seen = {}
    for kind in ("running", "waiting", "failed"):
        for m, pts in raw.get(kind, []):
            ns, pod = m.get("namespace"), m.get("pod", "")
            key = (ns, app_name(pod), where.get((ns, pod), ""))
            ts = [t for t, v in pts if v]
            if kind == "failed":
                ts = ts[:1]
            seen.setdefault((key, kind), []).extend(ts)
    segs = segments(seen, step, until)
    lines = {}
    jobs = set((week or {}).get("runs", {}))
    for ((ns, app, node), kind), ss in segs.items():
        if app in jobs and kind in ("running", "failed"):
            continue                                    # a job's bars come from its runs, below
        lines.setdefault((ns, app, node), []).extend([[a, b, kind] for a, b in ss])
    job_node = {}
    for cj, runs in (week or {}).get("runs", {}).items():
        nodes = [r["node"] for r in runs if r["node"]]
        usual = max(set(nodes), key=nodes.count) if nodes else ""
        job_node[cj] = usual
        for r in runs:
            state = "failed" if r["result"] in ("failed", "oom") else "running"
            # a run that is still going lasts until now; a failed one without an end is a mark at its start
            stop = r["end"] or (until if r["result"] == "running" else r["start"] + step)
            if stop < start:
                continue
            lines.setdefault(("apps", cj, r["node"] or usual), []).append([max(r["start"], start), stop, state])
    marks = {}
    for m, pts in raw.get("pushed", []):
        if pts:
            ns, pod = m.get("namespace"), m.get("pod", "")
            marks.setdefault((ns, app_name(pod), where.get((ns, pod), "")), []).append(
                {"t": pts[0][0], "text": "pushed out to make room" if m.get("reason") == "PreemptionByScheduler"
                 else "pushed off its machine (short of memory or disk)"})
    for m, pts in raw.get("oom", []):
        if pts:
            ns, pod = m.get("namespace"), m.get("pod", "")
            marks.setdefault((ns, app_name(pod), where.get((ns, pod), "")), []).append(
                {"t": pts[0][0], "text": "killed for using too much memory"})
    # a pod that waited for room had no machine yet: its wait goes on the row of the machine its app runs on
    home = {(ns, name): r.get("node") for (ns, name), r in rows_by_app.items() if r.get("node")}
    home.update({("apps", cj): n for cj, n in job_node.items() if n})
    for store in (lines, marks):
        for k in [k for k in store if not k[2] and home.get(k[:2])]:
            store.setdefault((k[0], k[1], home[k[:2]]), []).extend(store.pop(k))
    keys = set(lines) | set(marks)
    for (ns, name), r in rows_by_app.items():          # every app on a machine now, even with nothing to draw
        if r.get("live") and r.get("node"):
            keys.add((ns, name, r["node"]))
    out = {}
    for node in NODES + [""]:
        rows = []
        for k in [k for k in keys if k[2] == node]:
            ns, app, _ = k
            info = rows_by_app.get((ns, app)) or {}
            ss = sorted(lines.get(k, []))
            mk = sorted(marks.get(k, []), key=lambda x: x["t"])
            sc = (sched or {}).get(app) if ns == "apps" else None
            trouble = (info.get("state") in ("bad", "warn") or any(x[2] in ("failed", "waiting") for x in ss) or bool(mk)
                       or bool(sc and sc.get("low")))
            rows.append({"name": app, "ns": ns, "href": href(ns, app), "what": info.get("what", ""),
                         "rank": info.get("rank", "") if info else "", "rank_value": info.get("rank_value", 0),
                         "pushes": info.get("pushes", False), "booked": info.get("booked"), "mem": info.get("mem"),
                         "peak": info.get("peak"), "state": info.get("state", "ok"), "segs": ss, "marks": mk,
                         "ticks": sc["ticks"] if sc and len(sc["ticks"]) <= 120 else [],
                         "sched": sc, "system": is_system(P, ns, app), "trouble": trouble})
        rows.sort(key=lambda r: (-r["rank_value"], -(r["mem"] or r["peak"] or 0), r["name"]))
        out[node] = {"x0": start, "x1": until, "rows": rows}
    return out


def schedules(raw, week, P=None):
    """Each CronJob: its schedule in words, when it should have run in this range, how many times it did."""
    out = {}
    suspended = {m.get("cronjob") for m, _ in raw.get("cj_suspend", [])}
    runs = (week or {}).get("runs", {})
    for m, _ in raw.get("cj_info", []):
        cj, sched, tz = m.get("cronjob"), m.get("schedule", ""), m.get("timezone")
        due = [] if cj in suspended else cron.times(sched, tz, raw["start"], raw["end"])
        ran = sum(1 for r in runs.get(cj, []) if raw["start"] <= r["start"] <= raw["end"])
        missed = max(0, len(due) - ran - 1) if due else 0        # the one due as the range ends may not have started
        out[cj] = {"words": cron.words(sched, tz), "paused": cj in suspended, "due": len(due), "ran": ran,
                   "missed": missed, "ticks": due, "system": is_system(P, "apps", cj),
                   "low": bool(len(due) >= 3 and ran < 0.8 * len(due))}     # ran clearly less often than scheduled
    return out


def short_rank(rank):
    return {"system-node-critical": "k3s", "system-cluster-critical": "k3s", "homelab-critical": "critical",
            "homelab-normal": "normal", "batch-high": "batch high", "batch-mid": "batch mid", "batch-low": "batch low",
            "batch-make-room": "makes room", "steps-aside": "steps aside"}.get(rank, rank or "")


def kills(raw, week):
    """Killed for memory or pushed out in this range: when, which, by whom and why."""
    kernel = {}
    for m, pts in raw.get("kernel", []):
        kernel.setdefault(m.get("instance"), []).extend(t for t, v in pts if v)
    out = []
    for o in (week or {}).get("ooms", []):
        if not o["at"] or o["at"] < raw["start"]:
            continue
        node = ""
        for rs in (week or {}).get("runs", {}).values():
            for r in rs:
                if r["job"] and o["pod"].startswith(r["job"]):
                    node = r["node"]
        machine_out = any(abs(t - o["at"]) <= 900 for t in kernel.get(node, []))
        own = o["limit"] and o["peak"] and o["peak"] >= 0.9 * o["limit"]
        why = ("the machine ran out of memory and the kernel picked it" if machine_out and not own else
               f"it went over its own limit of {o['limit'] / G:.1f} GB" if o["limit"] else "it went over its memory limit")
        out.append({"at": o["at"], "name": o["name"], "kind": o["kind"], "what": "killed for memory", "why": why,
                    "node": node, "after": o.get("after")})
    for m, pts in raw.get("pushed", []):
        if pts:
            out.append({"at": pts[0][0], "name": app_name(m.get("pod", "")), "kind": "app",
                        "what": "pushed out", "node": "",
                        "why": "a pod of a higher rank needed its room" if m.get("reason") == "PreemptionByScheduler"
                        else "its machine ran short of memory or disk and pushed out its lowest-ranked pods"})
    for node, ts in kernel.items():
        for t in segments({node: ts}, raw["step"], raw["end"])[node]:
            out.append({"at": t[0], "name": MACHINE_NAMES.get(node, node), "kind": "machine",
                        "what": "ran out of memory", "why": "the kernel killed a program to free memory", "node": node})
    return sorted(out, key=lambda k: -k["at"])


def heat(raw, X):
    s = [{"name": MACHINE_NAMES[n], "values": one(raw, f"temp:{n}", X)} for n in NODES]
    gpu = one(raw, "gpu_temp", X)
    if any(v is not None for v in gpu):
        s.append({"name": "GPU (Acer)", "values": gpu})
    return {"kind": "lines", "unit": " °C", "x": X, "series": [x for x in s if any(v is not None for v in x["values"])],
            "lines": [{"name": "alarm (90 °C)", "values": [90] * len(X), "dash": True, "color": "var(--warn)"}]}


def gpu_users(raw, X, href):
    named = {}
    for m, pts in raw.get("gpu_pods", []):
        name = app_name(m.get("pod", ""))
        acc = named.setdefault(name, {})
        for t, v in pts:
            acc[int(t)] = acc.get(int(t), 0) + v
    series = [{"name": n, "values": [round(acc[t], 1) if t in acc else None for t in X], "href": href("apps", n)}
              for n, acc in named.items() if max(acc.values(), default=0) >= 1]
    series.sort(key=lambda s: -sum(v or 0 for v in s["values"]))
    return {"kind": "area", "stacked": True, "unit": "%", "x": X, "series": series, "yMax": 100}


def build(raw, week=None, app_rows=None, root="", catalog=None):
    from .overview import Projects               # overview imports memory and prom like this module; no cycle
    P = Projects(catalog or {})
    X = grid(raw)
    href = lambda ns, name: f"{root}/apps/{ns}/{name}" if ns and name else None
    rows_by_app = {}
    for r in app_rows or []:
        rows_by_app.setdefault((r["ns"], r["name"]), r)
    mem = {}
    for n in NODES:
        chart = memory_stack(raw, n, X, href)
        for sr in chart["series"]:
            m_ = re.match(r"^(.*?)( \(Docker\))?$", sr["name"])
            sr["system"] = bool(sr.get("href")) and is_system(P, sr["href"].split("/")[-2], m_.group(1)) \
                or sr["name"] == "k3s itself"
        mem[n] = {"name": MACHINE_NAMES[n], "chart": chart, "answer": memory_answer(chart, X)}
    sched = schedules(raw, week, P)
    return {"mem": mem, "tables": tables(raw, week, rows_by_app, href, P, sched), "sched": sched,
            "kills": kills(raw, week),
            "heat": heat(raw, X), "gpu": gpu_users(raw, X, href), "start": raw["start"], "end": raw["end"]}


@source("answers.raw", ttl=60)
def read(range_key):
    return fetch(range_key)
