"""Apps (lab.machaka.net/machines/apps): Grafana's "Who gets in", "Ranks and memory" and the Kubernetes pods and
global views, merged: which apps run where, at what rank, what they booked and really use, what stopped or restarted
them, and whether each machine has room.

  Room          each machine's room for pods, what the running pods booked and really use, the peak today, what
                waiting pods want; booked against used over time
  Every app     one row per app (per machine for a DaemonSet): running, waiting for room, can't start, restarting,
                finished; its rank, booking, real memory and peak, CPU, restarts and why it last stopped, and its last
                24 hours hour by hour (running, waiting for room, failed)
  Restarts      apps that restarted today and why (out of memory, an error, the machine restarting)
  The ranks     who may push whom out, and how many apps hold each rank now (k8s/cluster/priority.yaml, through
                catalog.json)

Memory per pod comes from memory.py (live pods only, peaks over 24 h); the rest from kube-state-metrics.
"""
import concurrent.futures
import datetime as dt
import time

from . import prom, source
from ..config import TZ
from .memory import G, MACHINE_NAMES, app_name
from .overview import OOM, Projects, dur

RANGES = {"24h": (24, 300, "5m"), "7d": (168, 1800, "30m")}
USED = 'container_memory_working_set_bytes{job="kubelet-cadvisor",id="/kubepods.slice"}'
BOOKED = ('sum by (node) (kube_pod_container_resource_requests{resource="memory"} '
          'and on (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1))')
WANTED = ('sum(kube_pod_container_resource_requests{resource="memory"} '
          'and on (namespace, pod) (kube_pod_status_phase{phase="Pending"} == 1))')
CPU = 'container_cpu_usage_seconds_total{job="kubelet-cadvisor",container!="",container!="POD"}'

Q = {
    "info": "kube_pod_info",
    "phase": "kube_pod_status_phase == 1",
    "ready": 'kube_pod_status_ready{condition="true"} == 1',
    "start": "kube_pod_start_time",
    "cstart": "max by (namespace, pod) (kube_pod_container_state_started)",
    "unschedulable": "kube_pod_status_unschedulable == 1",
    "created": "kube_pod_created",
    "cpu": f"sum by (namespace, pod) (rate({CPU}[5m]))",
    "cpu_req": 'sum by (namespace, pod) (kube_pod_container_resource_requests{resource="cpu"})',
    "restarts": "sum by (namespace, pod) (kube_pod_container_status_restarts_total)",
    "restarts_24h": "sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[24h]))",
    "last_reason": "kube_pod_container_status_last_terminated_reason == 1",
    "last_code": "kube_pod_container_status_last_terminated_exitcode",
    "last_when": "kube_pod_container_status_last_terminated_timestamp",
    "waiting": "kube_pod_container_status_waiting_reason == 1",
    "qos": "kube_pod_status_qos_class == 1",
    "oom": OOM,
    "alloc": 'kube_node_status_allocatable{resource="memory"}',
    "booked": BOOKED,
    "used": f"max by (node) ({USED})",
    "used_peak": f"max by (node) (max_over_time({USED}[24h]))",
    "wanted": WANTED,
    "suspended": "kube_cronjob_spec_suspend == 1",
}
STRIP = {          # always the last 24 hours, every 5 minutes: each app's hours
    "running": 'max by (namespace, pod) (kube_pod_status_phase{phase="Running"} == 1)',
    "waiting": "max by (namespace, pod) (kube_pod_status_unschedulable == 1)",
    "failed": 'max by (namespace, pod) (kube_pod_status_phase{phase="Failed"} == 1)',
}


def ranged(w):
    return {
        "alloc": 'max by (node) (kube_node_status_allocatable{resource="memory"})',
        "booked": BOOKED,
        "used": f"max by (node) (max_over_time({USED}[{w}]))",
        "wanted": WANTED,
        "restarts": f"sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[{w}]))",
    }


def fetch(range_key):
    hours, step, w = RANGES[range_key]
    raw, errors = {"strip": {}, "r": {}}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {("i", k): pool.submit(prom.query, e) for k, e in Q.items()}
        futs.update({("strip", k): pool.submit(prom.query_range, e, 24, 300) for k, e in STRIP.items()})
        futs.update({("r", k): pool.submit(prom.query_range, e, hours, step) for k, e in ranged(w).items()})
        for (kind, key), fut in futs.items():
            try:
                res = fut.result()
            except Exception as e:          # noqa: BLE001 - one gap, not a broken page
                res = []
                errors.append(e)
            (raw if kind == "i" else raw[kind])[key] = res
    if len(errors) > len(futs) // 2:
        raise errors[0]
    raw["at"] = time.time()
    return raw


# ---- words --------------------------------------------------------------------------------------------------------

WAIT_WORDS = {"CrashLoopBackOff": "keeps crashing", "ImagePullBackOff": "can't get its image",
              "ErrImagePull": "can't get its image", "ErrImageNeverPull": "its image isn't on the machine",
              "CreateContainerConfigError": "a setting or secret is missing", "CreateContainerError": "can't be created",
              "InvalidImageName": "its image name is wrong", "RunContainerError": "can't run"}
STARTING = {"ContainerCreating", "PodInitializing"}
STOP_WORDS = {"OOMKilled": "ran out of memory", "Error": "crashed", "Completed": "finished",
              "Unknown": "stopped by a machine restart", "ContainerStatusUnknown": "lost when the machine restarted",
              "Evicted": "pushed off its machine", "DeadlineExceeded": "ran past its time limit"}
KIND_WORDS = {"ReplicaSet": "app", "Deployment": "app", "StatefulSet": "app (numbered copies)",
              "DaemonSet": "one per machine", "Job": "job", "": "pod", "<none>": "pod", "Node": "k3s's own"}


def stop_words(reason, code):
    words = STOP_WORDS.get(reason, reason or "stopped")
    if reason == "Error" and code not in (None, 0):
        words += f" (exit code {code:.0f}{', killed' if code == 137 else ''})"
    return words


def hour_starts(now):
    """The starts of the last 24 local clock hours, oldest first (the last one is the hour we're in)."""
    top = dt.datetime.fromtimestamp(now, TZ).replace(minute=0, second=0, microsecond=0)
    return [(top - dt.timedelta(hours=23 - i)).timestamp() for i in range(24)]


def _keyed(rows):
    return {(m.get("namespace"), m.get("pod")): v for m, v in rows}


# ---- from numbers to the page (tests feed `raw` directly) --------------------------------------------------------

def build(raw, mem, catalog=None, now=None, range_key="24h"):
    now = now or raw.get("at") or time.time()
    catalog = catalog or {}
    P = Projects(catalog)
    notes = catalog.get("notes") or {}
    ranks = catalog.get("ranks") or []
    rank_by = {r["name"]: r for r in ranks}
    info = {(m.get("namespace"), m.get("pod")): m for m, _ in raw.get("info", [])}
    phase = {(m.get("namespace"), m.get("pod")): m.get("phase") for m, _ in raw.get("phase", [])}
    ready = {(m.get("namespace"), m.get("pod")) for m, _ in raw.get("ready", [])}
    unsched = {(m.get("namespace"), m.get("pod")) for m, _ in raw.get("unschedulable", [])}
    start, created = _keyed(raw.get("start", [])), _keyed(raw.get("created", []))
    cstart = _keyed(raw.get("cstart", []))            # its containers' last start: after a restart, "up" counts from it
    cpu, cpu_req = _keyed(raw.get("cpu", [])), _keyed(raw.get("cpu_req", []))
    restarts, restarts_24h = _keyed(raw.get("restarts", [])), _keyed(raw.get("restarts_24h", []))
    when = {}
    for m, v in raw.get("last_when", []):
        key = (m.get("namespace"), m.get("pod"), m.get("container"))
        when[key] = v
    code = {(m.get("namespace"), m.get("pod"), m.get("container")): v for m, v in raw.get("last_code", [])}
    last_stop = {}                       # (ns, pod) -> the latest stop of any of its containers
    for m, _ in raw.get("last_reason", []):
        k3 = (m.get("namespace"), m.get("pod"), m.get("container"))
        stop = {"reason": m.get("reason"), "code": code.get(k3), "at": when.get(k3)}
        k2 = k3[:2]
        if k2 not in last_stop or (stop["at"] or 0) > (last_stop[k2]["at"] or 0):
            last_stop[k2] = stop
    waiting = {(m.get("namespace"), m.get("pod")): m.get("reason") for m, _ in raw.get("waiting", [])}
    qos = {(m.get("namespace"), m.get("pod")): m.get("qos_class") for m, _ in raw.get("qos", [])}
    suspended = {(m.get("namespace"), m.get("cronjob")) for m, _ in raw.get("suspended", [])}
    oom = {}
    for m, _ in raw.get("oom", []):
        oom[(m.get("namespace"), app_name(m.get("pod", "")))] = True
    memrows = {r["detail"]: r for r in (mem or {}).get("rows", []) if r["kind"] == "pod" and r["running"]}
    gone_mem = {(r["detail"].split(" ")[0], r["name"], r["machine"]): r for r in (mem or {}).get("rows", [])
                if r["kind"] == "pod" and not r["running"]}

    # ---- the 24 hours, per app
    starts = hour_starts(now)
    def cell_of(t):
        i = int((t - starts[0]) // 3600)
        return i if 0 <= i < 24 else None
    hours = {}                           # (ns, app) -> [set of states per hour]
    ran_today = set()
    for kind in ("running", "waiting", "failed"):
        for labels, points in raw.get("strip", {}).get(kind, []):
            ns, pod = labels.get("namespace"), labels.get("pod", "")
            key = (ns, app_name(pod))
            cells = hours.setdefault(key, [set() for _ in range(24)])
            ts = [t for t, v in points if v]
            if kind == "failed":
                ts = ts[:1]              # a failed pod stays "Failed" until deleted: count the hour it failed
            for t in ts:
                i = cell_of(t)
                if i is not None:
                    cells[i].add(kind)
            if kind == "running" and ts:
                ran_today.add(key)

    # ---- group pods into apps
    apps = {}
    for (ns, pod), m in info.items():
        app = app_name(pod)
        kind = m.get("created_by_kind") or ""
        node = m.get("node") or ""
        key = (ns, app, node if kind == "DaemonSet" else "")
        a = apps.setdefault(key, {"ns": ns, "name": app, "kind": kind, "pods": [], "nodes": set(),
                                  "rank": m.get("priority_class") or ""})
        a["pods"].append(pod)
        if node:
            a["nodes"].add(node)
        if m.get("priority_class"):
            a["rank"] = m.get("priority_class")

    rows = []
    for (ns, app, dnode), a in apps.items():
        pods = a["pods"]
        live = [p for p in pods if phase.get((ns, p)) in ("Running", "Pending")]
        latest = max(pods, key=lambda p: created.get((ns, p)) or start.get((ns, p)) or 0)
        focus = live or [latest]
        node = dnode or next((info[(ns, p)].get("node") for p in focus if info[(ns, p)].get("node")), "")
        m_now = [memrows.get(f"{ns}/{p}") for p in live]
        m_now = [r for r in m_now if r]
        mem_now = sum(r["now"] or 0 for r in m_now) if m_now else None
        booked = sum(r["booked"] or 0 for r in m_now) if m_now else None
        limit = max((r["limit"] or 0 for r in m_now), default=0) or None
        peak = max([r["peak"] or 0 for r in m_now] + [r["peak"] for (gns, gapp, gnode), r in gone_mem.items()
                                                       if gns == ns and gapp == app and (not dnode or gnode == dnode)],
                   default=0) or None
        trend = m_now[0]["trend"] if len(m_now) == 1 else []
        stops = [last_stop[(ns, p)] for p in focus if (ns, p) in last_stop]
        stop = max(stops, key=lambda s: s["at"] or 0) if stops else None
        r24 = sum(restarts_24h.get((ns, p), 0) for p in live)
        wait_reason = next((waiting[(ns, p)] for p in live if waiting.get((ns, p))), None)
        pending = [p for p in live if phase.get((ns, p)) == "Pending"]
        no_room = [p for p in pending if (ns, p) in unsched]
        is_job = a["kind"] == "Job"
        rank = rank_by.get(a["rank"])
        cells = hours.get((ns, app), [set() for _ in range(24)])
        row = {
            "ns": ns, "name": app, "kind": a["kind"], "kind_words": KIND_WORDS.get(a["kind"], a["kind"].lower()),
            "node": node, "pods": sorted(pods), "live": sorted(live), "rank": a["rank"] or "(none)",
            "rank_value": rank["value"] if rank else 0, "pushes": bool(rank and rank["pushes"]),
            "what": notes.get(app) or notes.get(app.rsplit("-", 1)[0]) or "", "p": P.group(ns, app),
            "mem": mem_now, "booked": booked, "limit": limit, "peak": peak, "trend": trend,
            "cpu": sum(cpu.get((ns, p), 0) for p in live) if live else None,
            "cpu_req": sum(cpu_req.get((ns, p), 0) for p in live) or None,
            "restarts": sum(restarts.get((ns, p), 0) for p in live), "restarts_24h": round(r24),
            "stop": stop, "stop_words": stop_words(stop["reason"], stop["code"]) if stop else "",
            "qos": next((qos.get((ns, p)) for p in focus if qos.get((ns, p))), ""),
            "since": min((cstart.get((ns, p)) or start.get((ns, p)) for p in live
                          if cstart.get((ns, p)) or start.get((ns, p))), default=None),
            "is_job": is_job, "machine_name": MACHINE_NAMES.get(node, node or "not placed"),
            "cells": [{"state": "failed" if "failed" in c else "waiting" if "waiting" in c else
                       "running" if "running" in c else None, "start": starts[i]} for i, c in enumerate(cells)],
        }
        # state: the worst thing about it
        if wait_reason and wait_reason not in STARTING:
            row.update(state="bad", text=f"can't start: {WAIT_WORDS.get(wait_reason, wait_reason)}")
        elif no_room:
            age = now - (created.get((ns, no_room[0])) or now)
            row.update(state="warn" if age > 300 else "info", text=f"waiting {dur(age)} for room")
        elif pending:
            row.update(state="info", text="starting")
        elif live and not all((ns, p) in ready for p in live if phase.get((ns, p)) == "Running") and not is_job:
            row.update(state="warn", text="running, but not ready")
        elif oom.get((ns, app)) and live:
            row.update(state="warn", text="killed for using too much memory in the last 24 h; it restarted")
        elif r24 >= 3:
            row.update(state="warn", text=f"restarted {round(r24)} times in the last 24 h")
        elif live:
            row.update(state="running" if is_job else "ok",
                       text=("running for " if is_job else "up ") + dur(now - row["since"]) if row["since"] else "running")
        elif is_job and (ns, app) in suspended:          # turned off on purpose: no colour, nothing is wrong
            row.update(state="idle", text="paused (turned off on purpose)")
        elif is_job and phase.get((ns, latest)) == "Failed":
            row.update(state="warn", text="its last run failed")
        elif is_job:
            row.update(state="idle", text="finished" + (" (ran today)" if (ns, app) in ran_today else ""))
        else:
            row.update(state="idle", text=(phase.get((ns, latest)) or "stopped").lower())
        rows.append(row)
    order = {"bad": 0, "warn": 1, "info": 2, "running": 3, "ok": 3, "idle": 4}
    rows.sort(key=lambda r: (order.get(r["state"], 5), -r["rank_value"], -(r["mem"] or 0), r["name"]))

    # ---- each machine's room
    alloc, booked, used = (_node(raw.get(k, [])) for k in ("alloc", "booked", "used"))
    used_peak = _node(raw.get("used_peak", []))
    wanted = next((v for _, v in raw.get("wanted", [])), 0) or 0
    machines = []
    for node in sorted(alloc, key=lambda n: list(MACHINE_NAMES).index(n) if n in MACHINE_NAMES else 9):
        a = alloc[node]
        machines.append({"node": node, "name": MACHINE_NAMES.get(node, node), "alloc": a, "booked": booked.get(node, 0),
                         "used": used.get(node, 0), "peak": used_peak.get(node, 0),
                         "booked_share": booked.get(node, 0) / a if a else 0, "used_share": used.get(node, 0) / a if a else 0,
                         "peak_share": used_peak.get(node, 0) / a if a else 0,
                         "apps": sum(1 for r in rows if r["node"] == node and r["live"])})
    R = raw.get("r", {})
    room_graphs = {}
    for m in machines:
        node = m["node"]
        pick = lambda key: next((pts for labels, pts in R.get(key, []) if labels.get("node") == node), [])
        series = [{"name": "Room for pods", "slot": 8, "points": [(t, v / G) for t, v in pick("alloc")]},
                  {"name": "Booked", "slot": 1, "points": [(t, v / G) for t, v in pick("booked")]},
                  {"name": "Really used", "slot": 2, "points": [(t, v / G) for t, v in pick("used")]}]
        room_graphs[node] = [s for s in series if s["points"]]
    wanted_pts = next((pts for _, pts in R.get("wanted", [])), [])
    restart_lines = {}
    for labels, points in R.get("restarts", []):
        app = app_name(labels.get("pod", ""))
        acc = restart_lines.setdefault(app, {})
        for t, v in points:
            acc[t] = acc.get(t, 0) + v
    restart_lines = {k: sorted(v.items()) for k, v in restart_lines.items() if sum(v.values()) >= 0.5}
    ranked = sorted(restart_lines.items(), key=lambda kv: -sum(v for _, v in kv[1]))[:5]
    restarts_graph = [{"name": name, "slot": i + 1, "points": [(t, round(v, 2)) for t, v in pts]}
                      for i, (name, pts) in enumerate(ranked)]

    restarted = sorted((r for r in rows if r["restarts_24h"] >= 1 or (r["stop"] and r["stop"]["at"]
                                                                       and now - r["stop"]["at"] < 86400)),
                       key=lambda r: -((r["stop"] or {}).get("at") or 0))
    # Apps stopped by their machine restarting come back with it: one line per machine, not one per app.
    reboots = {}
    for r in restarted:
        if r["stop"] and r["stop"]["reason"] in ("Unknown", "ContainerStatusUnknown"):
            b = reboots.setdefault(r["node"], {"node": r["node"], "name": MACHINE_NAMES.get(r["node"], r["node"]),
                                               "apps": [], "at": None})
            b["apps"].append(r["name"])
            b["at"] = max(b["at"] or 0, r["since"] or 0) or None
    restarted = [r for r in restarted if not (r["stop"] and r["stop"]["reason"] in ("Unknown", "ContainerStatusUnknown"))]
    held = {}
    for r in rows:
        if r["live"]:
            held[r["rank"]] = held.get(r["rank"], 0) + 1
    ladder = [{**rk, "apps": held.get(rk["name"], 0)} for rk in ranks]
    if held.get("(none)"):
        ladder.append({"name": "(none)", "value": 0, "pushes": False, "default": False, "apps": held["(none)"],
                       "what": "Pods made before the default rank existed; they get it at their next restart."})
    return {"rows": rows, "machines": machines, "room_graphs": room_graphs, "wanted": wanted,
            "reboots": sorted(reboots.values(), key=lambda b: -(b["at"] or 0)),
            "wanted_graph": [{"name": "Wanted by waiting pods", "slot": 4, "points": [(t, v / G) for t, v in wanted_pts]}]
            if any(v for _, v in wanted_pts) else [], "restarted": restarted, "restarts_graph": restarts_graph,
            "ladder": ladder, "hour_starts": starts, "groups": P.groups, "at": now, "range": range_key}


def _node(rows):
    out = {}
    for m, v in rows:
        n = m.get("node") or m.get("instance") or ""
        out[n] = out.get(n, 0) + v
    return out


@source("apps.raw", ttl=60)
def read(range_key):
    return fetch(range_key)
