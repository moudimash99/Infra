"""The overview (lab.machaka.net/machines): the questions the Grafana dashboards answer, answered in plain words.

  Anything wrong?          alerts firing, failed and late jobs, out-of-memory kills, pods waiting for room, sites down,
                           machines low on memory or disk or hot, PC tasks failing
  How are the machines?    per machine: memory, CPU, fullest disk, temperature, each with who or what is behind it
  What is using them?      the biggest users of memory and of CPU
  Did the jobs run?        every CronJob: failed or late first, then running, then fine
  Who gets in?             pods waiting for room, pods pushed out to make room, each machine's booking
  The GPU, the sites, the PC's tasks

Each item carries the machine it is on and its project (the lab.machaka.net menu group, from catalog.json, which
scripts/menu.py makes from services.yaml), so the page can filter every card the same way. The queries are those of
the Grafana dashboards (k8s/monitoring/dashboards/make.py); memory comes from memory.py, live pods only.
"""
import concurrent.futures
import datetime as dt
import json
import logging
import re
import time

from . import Reading, k8s, memory, misc, prom, source
from ..config import TZ, env
from .memory import G, MACHINE_NAMES, app_name, node_of

CATALOG_FILE = env("LAB_CATALOG", "/etc/lab-catalog/catalog.json")
PC_STALE = int(env("PC_STALE_MINUTES", "20")) * 60
OOM = ('(max by (namespace, pod, container) (max_over_time(kube_pod_container_status_terminated_reason'
       '{reason="OOMKilled"}[24h])) or max by (namespace, pod, container) (max_over_time('
       'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}[24h]))) > 0')
GPU_POD = ('sum by (pod) (gpu_process_sm_percent * on (pod_uid) group_left (pod) '
           'label_replace(kube_pod_info, "pod_uid", "$1", "uid", "(.+)"))')
FS = 'fstype=~"ext4|xfs|btrfs"'

Q = {
    # machines
    "up": 'up{job="node-exporter"}',
    "cpu": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
    "cores": 'count by (instance) (node_cpu_seconds_total{mode="idle"})',
    "disk": f'100 * (1 - node_filesystem_avail_bytes{{{FS}}} / node_filesystem_size_bytes{{{FS}}})',
    "disk_free": f'node_filesystem_avail_bytes{{{FS}}}',
    "temp": "max by (instance) (node_hwmon_temp_celsius)",
    "uptime": "time() - node_boot_time_seconds",
    # who uses the CPU (cores)
    "pod_cpu": ('sum by (namespace, pod, node) (rate(container_cpu_usage_seconds_total{job="kubelet-cadvisor",'
                'container!="",container!="POD"}[5m]))'),
    "k3s_cpu": ('sum by (node) (rate(container_cpu_usage_seconds_total{job="kubelet-cadvisor",'
                f'id=~"{memory.K3S_CGROUP}"}}[5m]))'),
    "docker_cpu": 'sum by (name) (rate(container_cpu_usage_seconds_total{job="cadvisor-docker",name!=""}[5m]))',
    # jobs
    "cj_suspend": 'kube_cronjob_spec_suspend{namespace="apps"}',
    "cj_success": 'kube_cronjob_status_last_successful_time{namespace="apps"}',
    "cj_schedule": 'kube_cronjob_status_last_schedule_time{namespace="apps"}',
    "cj_next": 'kube_cronjob_next_schedule_time{namespace="apps"}',
    "job_owner": 'kube_job_owner{namespace="apps",owner_kind="CronJob"}',
    "job_start": 'kube_job_status_start_time{namespace="apps"}',
    "job_end": 'kube_job_status_completion_time{namespace="apps"}',
    "job_failed": 'kube_job_failed{namespace="apps",condition="true"} == 1',
    "job_active": 'kube_job_status_active{namespace="apps"} > 0',
    "job_node": 'kube_pod_info{namespace="apps",created_by_kind="Job"}',
    "oom": OOM,
    # who gets in
    "pending": ('(time() - kube_pod_created) and on (namespace, pod) (kube_pod_status_phase{phase="Pending"} == 1)'),
    "pending_req": ('sum by (namespace, pod) (kube_pod_container_resource_requests{resource="memory"}) '
                    'and on (namespace, pod) (kube_pod_status_phase{phase="Pending"} == 1)'),
    "pod_info": "kube_pod_info",
    "restarts_24h": 'sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[24h]))',
    # the GPU
    "gpu_util": "nvidia_smi_utilization_gpu_ratio * 100",
    "gpu_mem": "nvidia_smi_memory_used_bytes",
    "gpu_mem_total": "nvidia_smi_memory_total_bytes",
    "gpu_temp": "nvidia_smi_temperature_gpu",
    "gpu_pods": GPU_POD,
    # the PC (its reports, read back by Prometheus from the lab)
    "pc_age": "time() - pc_report_timestamp_seconds",
    "pc_task_ok": "pc_task_ok",
    "pc_task_last": "pc_task_last_run_timestamp_seconds",
}
NAMESPACE_GROUP = {"monitoring": "Monitoring", "kube-system": "Platform", "flux-system": "Platform"}
SITE_SERVICE = {"machaka.net": "website", "Cat weight": "cat-weight", "Unmatched": "unmatched",
                "BoardXplorer": "boardxplorer", "FreeMotion": "freemotion-hub"}


# ---- small words -------------------------------------------------------------------------------------------------

def dur(seconds):
    """3 min, 5 h, 2 d: how long, rounded the way a person says it."""
    if seconds is None:
        return "?"
    s = abs(seconds)
    if s < 90:
        return f"{int(s)} s"
    if s < 90 * 60:
        return f"{round(s / 60)} min"
    if s < 36 * 3600:
        h = s / 3600
        return f"{h:.1f} h".replace(".0 h", " h") if h < 10 else f"{round(h)} h"
    return f"{round(s / 86400)} d"


def size(v):
    if v is None:
        return "?"
    return f"{v / G:.1f} GB" if v >= 0.995 * G else f"{v / 1024 ** 2:.0f} MB"


def clock(ts):
    if not ts:
        return "?"
    when = dt.datetime.fromtimestamp(ts, TZ)
    today = dt.datetime.now(TZ).date()
    if when.date() == today:
        return f"{when:%H:%M}"
    if when.date() == today + dt.timedelta(days=1):
        return f"tomorrow {when:%H:%M}"
    if when.date() == today - dt.timedelta(days=1):
        return f"yesterday {when:%H:%M}"
    return f"{when.day} {when:%b} {when:%H:%M}"


def machine_name(node):
    return MACHINE_NAMES.get(node, "PC" if node == "pc" else node or "")


# ---- projects ----------------------------------------------------------------------------------------------------

class Projects:
    """Which project (lab.machaka.net menu group) a pod, job, container or PC task belongs to."""

    def __init__(self, catalog):
        self.groups = catalog.get("groups") or []
        self.services = catalog.get("services") or {}
        self.known = {}                                     # (namespace or "docker" or "pc", name) -> object
        for key, obj in (catalog.get("objects") or {}).items():
            parts = key.split("/")
            if len(parts) == 3:
                self.known[(parts[0], parts[2])] = obj
            elif ":" in key:
                kind, name = key.split(":", 1)
                self.known[({"pc_task": "pc"}.get(kind, kind), name)] = obj

    def obj(self, ns, name):
        """The catalog object for a workload: by name, else the longest known name it starts with
        (bg-taste-match-gpu-2gib-1432 is bg-taste-match-gpu's), else one whose first word it shares when they all
        agree (fm-webllm and the other fm-* are Free Motion)."""
        if (ns, name) in self.known:
            return self.known[(ns, name)]
        starts = [(n, o) for (s, n), o in self.known.items() if s == ns and name.startswith(n + "-")]
        if starts:
            return max(starts, key=lambda t: len(t[0]))[1]
        word = name.split("-")[0]
        same = {o["service"] for (s, n), o in self.known.items() if s == ns and n.split("-")[0] == word}
        groups = {self.services.get(svc, {}).get("group") for svc in same}
        if len(groups) == 1 and same:
            return {"service": sorted(same)[0]}
        return None

    def group(self, ns, name):
        o = self.obj(ns, name)
        if o:
            return self.services.get(o["service"], {}).get("group", "")
        return NAMESPACE_GROUP.get(ns, "")

    def max_age_h(self, ns, name):
        o = self.known.get((ns, name))
        return o.get("max_age_h") if o else None


def load_catalog():
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ---- reading -----------------------------------------------------------------------------------------------------

def _pushed_out():
    """Pods pushed out to make room (events Preempted; Kubernetes keeps events about an hour): [(time, pod, by uid,
    node)]. The lab may read events only in apps."""
    try:
        items = k8s.get("/api/v1/namespaces/apps/events", fieldSelector="reason=Preempted").get("items", [])
    except Exception:                       # noqa: BLE001 - no events is not an error on the page
        return []
    out = []
    for e in items:
        m = re.search(r"Preempted by pod ([0-9a-f-]+)(?: on node (\S+))?", e.get("message", ""))
        when = e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp")
        out.append({"at": _ts(when), "pod": (e.get("involvedObject") or {}).get("name", "?"),
                    "by_uid": m.group(1) if m else None, "node": m.group(2) if m else None})
    return out


def _ts(iso):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fetch():
    """Every number the overview needs: {key: [(labels, value)]} plus alerts, sites, events and the memory page's
    reading. Queries run side by side (about 40, each a few milliseconds)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {key: pool.submit(prom.query, expr) for key, expr in Q.items()}
        pushed = pool.submit(_pushed_out)
        raw = {}
        for key, fut in futures.items():
            try:
                raw[key] = fut.result()
            except Exception:               # noqa: BLE001 - one missing exporter leaves one gap, not a broken page
                raw[key] = []
        raw["pushed"] = pushed.result()
    alerts, sites = prom.alerts.get(), misc.sites.get()
    raw["alerts"] = alerts.data if alerts.ok else None
    raw["sites"] = sites.data if sites.ok else None
    mem = memory.now.get()
    raw["memory"] = mem.data
    raw["at"] = time.time()
    return raw


# ---- from numbers to answers (no network here: tests feed `raw` directly) -----------------------------------------

def _one(rows, label="instance"):
    return {(m.get(label) or node_of(m)): v for m, v in rows}


def build(raw, catalog=None, now=None):
    now = now or raw.get("at") or time.time()
    P = Projects(catalog or {})
    mem = raw.get("memory") or {"machines": [], "rows": []}
    pod_node, pod_group, uid_pod, prio = {}, {}, {}, {}
    for m, _ in raw.get("pod_info", []):
        key = (m.get("namespace"), m.get("pod"))
        pod_node[key] = m.get("node") or ""
        uid_pod[m.get("uid")] = key
        prio[key] = m.get("priority_class") or ""
    def group_of(ns, pod):
        if (ns, pod) not in pod_group:
            pod_group[(ns, pod)] = P.group(ns, app_name(pod))
        return pod_group[(ns, pod)]

    problems = []
    def problem(sev, text, why="", m="", p="", href="", since=None):
        problems.append({"state": sev, "text": text, "why": why, "m": m, "p": p, "href": href, "since": since})

    # -- machines
    up = _one(raw.get("up", []))
    cpu, cores, temp, uptime = (_one(raw.get(k, [])) for k in ("cpu", "cores", "temp", "uptime"))
    disks = {}
    free_by = {(m.get("instance"), m.get("mountpoint")): v for m, v in raw.get("disk_free", [])}
    for m, v in raw.get("disk", []):
        inst = m.get("instance")
        if inst not in disks or v > disks[inst]["pct"]:
            disks[inst] = {"pct": v, "mount": m.get("mountpoint", "?"), "free": free_by.get((inst, m.get("mountpoint")))}
    cpu_rows = []
    for m, v in raw.get("pod_cpu", []):
        ns, pod, node = m.get("namespace"), m.get("pod"), node_of(m)
        if (ns, pod) in pod_node and v > 0.001:
            cpu_rows.append({"name": app_name(pod), "kind": "pod", "machine": node, "cores": v,
                             "p": group_of(ns, pod), "detail": f"{ns}/{pod}"})
    for m, v in raw.get("k3s_cpu", []):
        cpu_rows.append({"name": "k3s", "kind": "k3s", "machine": node_of(m), "cores": v, "p": "Platform",
                         "detail": "Kubernetes itself"})
    for m, v in raw.get("docker_cpu", []):
        if v > 0.001:
            cpu_rows.append({"name": m.get("name", "?"), "kind": "docker", "machine": memory.DOCKER_NODE, "cores": v,
                             "p": P.group("docker", m.get("name", "")), "detail": "Docker"})
    for r in cpu_rows:
        r["share"] = r["cores"] / cores[r["machine"]] if cores.get(r["machine"]) else None
    cpu_rows.sort(key=lambda r: -r["cores"])

    mem_rows = []
    for r in mem.get("rows", []):
        if not r.get("running") or not r.get("now") or r["kind"] == "program":
            continue
        ns, _, pod = r["detail"].partition("/") if r["kind"] == "pod" else ("", "", "")
        p = (group_of(ns, pod) if r["kind"] == "pod" else "Platform" if r["kind"] == "k3s"
             else P.group("docker", r["name"]))
        mem_rows.append({**r, "p": p})

    machines = []
    for m in mem.get("machines", []):
        node = m["node"]
        top_mem = next((r for r in mem_rows if r["machine"] == node), None)
        top_cpu = next((r for r in cpu_rows if r["machine"] == node), None)
        avail_share = m["available"] / m["total"] if m["total"] else 0
        d = disks.get(node) or {}
        t = temp.get(node)
        state = "ok"
        facts = {
            "mem": {"value": f"{size(m['available'])} free", "of": f"of {size(m['total'])}",
                    "why": f"mostly {top_mem['name']} ({size(top_mem['now'])})" if top_mem else "",
                    "pct": 100 * (1 - avail_share), "state": "bad" if avail_share < 0.12 else
                    "warn" if avail_share < 0.25 else "ok"},
            "cpu": {"value": f"{cpu.get(node, 0):.0f}% busy", "of": f"of {cores.get(node, 0):.0f} cores",
                    "why": (f"mostly {top_cpu['name']} ({top_cpu['cores']:.1f} cores)" if top_cpu
                            and top_cpu["cores"] >= 0.1 else "nothing busy"),
                    "pct": cpu.get(node, 0), "state": "warn" if cpu.get(node, 0) > 90 else "ok"},
            "disk": {"value": f"{d.get('pct', 0):.0f}% full", "of": f"{size(d.get('free'))} left",
                     "why": f"fullest: {d.get('mount', '?')}", "pct": d.get("pct", 0),
                     "state": "bad" if d.get("pct", 0) > 90 else "warn" if d.get("pct", 0) > 80 else "ok"},
            # the alert rules' limits (alert-rules.yml): warning above 90 °C, critical above 97 °C
            "temp": {"value": f"{t:.0f} °C" if t is not None else "?", "of": "hottest sensor", "why": "",
                     "pct": min(100, (t or 0)), "state": "bad" if (t or 0) > 97 else "warn" if (t or 0) > 90 else "ok"},
        }
        if up.get(node) == 0:
            state = "bad"
            problem("bad", f"{m['name']} isn't answering", "node-exporter is down: the machine may be off", m=node,
                    p="*")
        for key, f in facts.items():
            if f["state"] != "ok":
                state = "bad" if "bad" in (state, f["state"]) else "warn"
                label = {"mem": "low on memory", "cpu": "very busy", "disk": "disk filling up", "temp": "running hot"}[key]
                problem(f["state"], f"{m['name']} {label}: {f['value']}", f["why"], m=node, p="*", href="memory")
        booked_share = m["booked"] / m["allocatable"] if m["allocatable"] else 0
        machines.append({"node": node, "name": m["name"], "state": state, "facts": facts,
                         "uptime": uptime.get(node), "booked": m["booked"], "allocatable": m["allocatable"],
                         "booked_share": booked_share, "used_pods": m["parts"]["pods"],
                         "available": m["available"], "total": m["total"]})

    # -- the PC
    pc_age = next((v for _, v in raw.get("pc_age", [])), None)
    pc_last = {m.get("task"): v for m, v in raw.get("pc_task_last", [])}
    pc_tasks = []
    for m, v in raw.get("pc_task_ok", []):
        task = m.get("task", "?")
        name = task.lstrip("\\")
        ok = v >= 1
        pc_tasks.append({"name": name, "state": "ok" if ok else "bad", "status": m.get("status", ""),
                         "note": m.get("note", ""), "last": pc_last.get(task), "m": "pc",
                         "p": P.group("pc", task) or P.group("pc", "\\" + name)})
        if not ok:
            problem("warn", f"PC task {name} failed", m.get("note") or m.get("status", ""), m="pc",
                    p=pc_tasks[-1]["p"], href="#pc")
    pc_tasks.sort(key=lambda t: (t["state"] == "ok", t["name"].lower()))
    pc = {"age": pc_age, "stale": pc_age is None or pc_age > PC_STALE, "tasks": pc_tasks}

    # -- jobs
    owner = {m.get("job_name"): m.get("owner_name") for m, _ in raw.get("job_owner", [])}
    start = {m.get("job_name"): v for m, v in raw.get("job_start", [])}
    end = {m.get("job_name"): v for m, v in raw.get("job_end", [])}
    failed = {m.get("job_name") for m, _ in raw.get("job_failed", [])}
    active = {m.get("job_name") for m, _ in raw.get("job_active", [])}
    job_node = {}
    for m, _ in raw.get("job_node", []):
        job_node[m.get("created_by_name")] = m.get("node") or job_node.get(m.get("created_by_name"), "")
    ooms = {}
    for m, _ in raw.get("oom", []):
        ooms.setdefault(app_name(m.get("pod", "")), []).append(m)
    runs = {}
    for job, cj in owner.items():
        runs.setdefault(cj, []).append(job)
    cronjobs = {m.get("cronjob") for k in ("cj_suspend", "cj_schedule", "cj_next") for m, _ in raw.get(k, [])}
    suspend, success, sched, nxt = ({m.get("cronjob"): v for m, v in raw.get(k, [])}
                                    for k in ("cj_suspend", "cj_success", "cj_schedule", "cj_next"))
    jobs = []
    for cj in sorted(c for c in cronjobs if c):
        mine = sorted(runs.get(cj, []), key=lambda j: start.get(j) or 0, reverse=True)
        running = [j for j in mine if j in active]
        finished = [j for j in mine if j not in active and (j in failed or j in end)]
        last = finished[0] if finished else None
        last_failed = bool(last and last in failed)
        interval = (nxt[cj] - sched[cj]) if cj in nxt and cj in sched and nxt[cj] > sched[cj] else None
        limit_h = P.max_age_h("apps", cj) or ((2 * interval + 3600) / 3600 if interval else None)
        since_good = now - success[cj] if success.get(cj) else None
        node = job_node.get(mine[0], "") if mine else ""
        p = P.group("apps", cj)
        oom = ooms.get(cj)
        item = {"name": cj, "m": node, "p": p, "next": nxt.get(cj), "interval": interval,
                "since_good": since_good, "limit_h": limit_h, "last_start": start.get(last) if last else None,
                "took": (end[last] - start[last]) if last and last in end and last in start else None,
                "running_for": now - start[running[0]] if running and running[0] in start else None, "oom": bool(oom)}
        if suspend.get(cj):
            item.update(state="paused", text="paused", why="turned off on purpose")
        elif last_failed:
            item.update(state="bad", text=f"failed {dur(now - (start.get(last) or now))} ago",
                        why="killed for using too much memory" if oom else "its last run failed")
            problem("bad", f"{cj} failed", item["why"] + (f"; last good run {dur(since_good)} ago" if since_good
                                                         else "; no good run yet"), m=node, p=p, href="jobs",
                    since=start.get(last))
        elif limit_h and (since_good is None or since_good > limit_h * 3600):
            item.update(state="warn", text=f"late: last good run {dur(since_good)} ago" if since_good else "never ran well",
                        why=f"expected within {limit_h:g} h" + (", running now" if running else ""))
            problem("warn", f"{cj} is late", f"last good run {dur(since_good)} ago, expected within {limit_h:g} h"
                    + (" (running now)" if running else ""), m=node, p=p, href="jobs")
        elif running:
            item.update(state="running", text=f"running for {dur(item['running_for'])}",
                        why=f"last good run {dur(since_good)} ago" if since_good else "")
        else:
            item.update(state="ok", text=f"ran well {dur(since_good)} ago" if since_good else "ok",
                        why=f"next {clock(nxt.get(cj))}" if nxt.get(cj) else "")
        jobs.append(item)
    order = {"bad": 0, "warn": 1, "running": 2, "ok": 3, "paused": 4}
    jobs.sort(key=lambda j: (order[j["state"]], j["name"]))
    for name, pods in ooms.items():                  # apps (not jobs) killed for memory
        if name in cronjobs:
            continue
        ns = pods[0].get("namespace", "")
        problem("warn", f"{name} was killed for using too much memory", "in the last 24 h; it restarted",
                m=pod_node.get((ns, pods[0].get("pod")), ""), p=P.group(ns, name), href="memory")

    # -- who gets in
    req = {(m.get("namespace"), m.get("pod")): v for m, v in raw.get("pending_req", [])}
    waiting = []
    for m, age in raw.get("pending", []):
        ns, pod = m.get("namespace"), m.get("pod")
        name = app_name(pod)
        need = req.get((ns, pod))
        item = {"name": name, "pod": pod, "age": age, "need": need, "m": pod_node.get((ns, pod), ""),
                "p": group_of(ns, pod), "rank": prio.get((ns, pod), ""), "state": "warn" if age > 300 else "info"}
        waiting.append(item)
        if age > 300:
            problem("warn", f"{name} has been waiting for room for {dur(age)}",
                    f"it books {size(need)}; no machine has that much unbooked" if need else "no machine has room",
                    p=item["p"], href="#gets-in")
    waiting.sort(key=lambda w: -w["age"])
    pushed = []
    for e in raw.get("pushed", []):
        if not e.get("at") or now - e["at"] > 3600:
            continue
        by = uid_pod.get(e.get("by_uid"))
        pushed.append({"name": app_name(e["pod"]), "pod": e["pod"], "at": e["at"],
                       "by": app_name(by[1]) if by else "a higher-ranked pod", "m": e.get("node") or "",
                       "p": P.group("apps", app_name(e["pod"])), "state": "info"})
    pushed.sort(key=lambda e: -e["at"])

    # -- alerts
    for a in raw.get("alerts") or []:
        lab = a.get("labels", {})
        node = lab.get("node") or lab.get("instance", "").split(":")[0]
        node = {"192.168.1.116": "moudimash99", "192.168.1.93": "acer-laptop"}.get(node, node)
        problem("bad" if a.get("severity") == "critical" else "warn", a.get("summary") or a["name"],
                f"alert {a['name']}", m=node if node in MACHINE_NAMES else "", p="*", since=_ts(a.get("since")))

    # -- sites
    sites = []
    for s in raw.get("sites") or []:
        svc = SITE_SERVICE.get(s["name"])
        p = P.services.get(svc, {}).get("group", "") if svc else ""
        st = "ok" if s.get("up") else "unknown" if s.get("up") is None else "bad"
        sites.append({**s, "state": st, "p": p, "m": ""})
        if st == "bad":
            problem("bad", f"{s['name']} is down", f"answered {s.get('code') or s.get('error', 'nothing')}", p=p,
                    href=s.get("url", ""))

    # -- the GPU
    one = lambda k: next((v for _, v in raw.get(k, [])), None)
    gpu = None
    if raw.get("gpu_util"):
        users = sorted(((app_name(m.get("pod", "?")), v) for m, v in raw.get("gpu_pods", []) if v > 0),
                       key=lambda u: -u[1])
        # the card's busy figure and the per-process one are sampled apart; the larger is the honest "how busy"
        gpu = {"util": max(one("gpu_util") or 0, sum(v for _, v in users)), "mem": one("gpu_mem"), "mem_total": one("gpu_mem_total"),
               "temp": one("gpu_temp"), "users": users, "m": "acer-laptop"}
        if (gpu["temp"] or 0) > 85:
            problem("warn", f"The GPU is hot: {gpu['temp']:.0f} °C", "", m="acer-laptop", p="*")

    rank = {"bad": 0, "warn": 1, "info": 2}
    problems.sort(key=lambda x: (rank.get(x["state"], 3), -(x["since"] or 0)))
    TOP = 12           # the overview shows the biggest users only; the Memory tab has everything
    return {"problems": problems, "machines": machines, "pc": pc, "mem_rows": mem_rows[:TOP],
            "cpu_rows": cpu_rows[:TOP], "n_users": len(mem_rows),
            "jobs": jobs, "waiting": waiting, "pushed": pushed, "sites": sites, "gpu": gpu,
            "groups": P.groups, "at": now}


@source("overview", ttl=150)
def now():
    return build(fetch(), load_catalog())


def keep_fresh():
    """Read everything again now (main.serve calls this every minute), so a visitor finds numbers at most a minute
    old and never waits for the ~40 queries; `now` reads them itself only if this stops."""
    try:
        now._cache[()] = Reading(True, build(fetch(), load_catalog()), "", time.time())
    except Exception:                      # noqa: BLE001 - the page then reads on its own and shows the error
        logging.getLogger("lab").exception("overview refresh failed")
