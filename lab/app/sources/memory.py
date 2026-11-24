"""Where each machine's memory goes, from Prometheus (lab.machaka.net/machines).

One question: "this machine is low on memory: who is using it?". Each machine's memory is split into parts measured
the same way (cgroup working set, from the kubelet's cAdvisor), so they add up to the machine's size exactly:

  pods     the k3s pods' cgroup (/kubepods.slice): only what is running, never a dead pod
  k3s      Kubernetes itself (/system.slice/k3s.service or k3s-agent.service: k3s, containerd, the shims)
  docker   the server's Docker containers (the cAdvisor in Docker on 192.168.1.116, same kernel counters)
  other    everything else programs use: the root cgroup ("/") minus the three above (system services, ssh, ...)
  kernel   used (total minus available, the kernel's own estimate) minus the root cgroup, when above zero: memory
           the kernel holds that no cgroup is charged for (drivers, the Acer's GPU, shared memory)
  cache    file cache the kernel can hand back: what is left between the root cgroup + kernel and free
  free     not used at all

Programs outside containers (process-exporter) are listed in the table to explain "other", but not added to the
bar: they are resident memory, a different measure that overlaps the cgroups (shared libraries, mapped files).
If pods + k3s + docker ever exceed the root cgroup, "other" is shown as 0 and the overlap is reported.

Per-pod numbers are counted for live pods only (kube-state-metrics phase Running or Pending): Prometheus keeps a
replaced pod's series for about 5 minutes, and summing those would count a rolled-out app twice.
"""
import re
import time

from . import source
from ..config import PROMETHEUS
from .prom import _http, query, query_range

G = 1024 ** 3
DOCKER_NODE = "moudimash99"          # the cAdvisor-in-Docker target is the server (prometheus.yml, cadvisor-docker)
MACHINE_NAMES = {"moudimash99": "Server", "acer-laptop": "Acer"}
K3S_CGROUP = r"/system.slice/k3s(-agent)?\\.service"   # PromQL strings need the backslash doubled
WS = 'container_memory_working_set_bytes{job="kubelet-cadvisor"'
POD_WS = f'sum by (namespace, pod, node) ({WS},container!="",container!="POD"}})'
DOCKER_WS = 'container_memory_working_set_bytes{job="cadvisor-docker",name!=""}'
PROGRAMS = "namedprocess_namegroup_memory_bytes"

NOW = {
    "total": "node_memory_MemTotal_bytes",
    "available": "node_memory_MemAvailable_bytes",
    "free": "node_memory_MemFree_bytes",
    "swap": "node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes",
    "root": f'{WS},id="/"}}',
    "pods": f'{WS},id="/kubepods.slice"}}',
    "k3s": f'{WS},id=~"{K3S_CGROUP}"}}',
    "allocatable": 'kube_node_status_allocatable{resource="memory"}',
    # Waiting for room for over 5 minutes: a short wait (the archiver every 3 minutes) is normal, not a problem.
    "unschedulable": 'kube_pod_status_unschedulable == 1 and on (namespace, pod) (time() - kube_pod_created) > 300',
    "pod_ws": POD_WS,
    "pod_peak": f"max_over_time({POD_WS}[24h:5m])",
    "live": 'max by (namespace, pod) (kube_pod_status_phase{phase=~"Running|Pending"}) == 1',
    "requests": 'sum by (namespace, pod) (kube_pod_container_resource_requests{resource="memory"})',
    "limits": 'sum by (namespace, pod) (kube_pod_container_resource_limits{resource="memory"})',
    "docker": DOCKER_WS,
    "docker_peak": f"max_over_time({DOCKER_WS}[24h])",
    "programs": PROGRAMS,
    "programs_peak": f"max_over_time({PROGRAMS}[24h])",
    "k3s_peak": f'max_over_time({WS},id=~"{K3S_CGROUP}"}}[24h])',
}
TRENDS = {                            # 24 h, one point every 20 minutes: the small line in each table row
    "pod_ws": POD_WS,
    "docker": DOCKER_WS,
    "k3s": f'{WS},id=~"{K3S_CGROUP}"}}',
    "available": "node_memory_MemAvailable_bytes",
}
TREND_HOURS, TREND_STEP = 24, 1200

# Deployment pods end in -<replicaset hash>-<5>, CronJob runs in -<8 digits>-<5>, DaemonSet pods in -<5>.
_POD_SUFFIX = [re.compile(r"^(.+)-[0-9a-f]{6,10}-[a-z0-9]{5}$"), re.compile(r"^(.+)-\d{8}-[a-z0-9]{5}$"),
               re.compile(r"^(.+)-[a-z0-9]{5}$")]


def app_name(pod):
    """fm-controller-88b97b7f-t4xdl -> fm-controller; cat-flash-29924600-gc777 -> cat-flash."""
    for rx in _POD_SUFFIX:
        m = rx.match(pod)
        if m:
            return m.group(1)
    return pod


def node_of(labels):
    return labels.get("node") or labels.get("instance") or ""


def _program_counted_elsewhere(group):
    """process-exporter's group is "program|[cgroup]". Programs inside k3s's cgroup are in the k3s part already;
    "[/]" is process-exporter itself, seen from inside its own pod."""
    return bool(re.search(r"k3s(-agent)?\.service|\|\[/\]$", group))


def fetch():
    """Every number the page needs, as Prometheus returns them: {key: [(labels, value)]} plus trends."""
    raw = {key: query(expr) for key, expr in NOW.items()}
    raw["trends"] = {key: query_range(expr, TREND_HOURS, TREND_STEP) for key, expr in TRENDS.items()}
    raw["at"] = time.time()
    return raw


def low_point(node, hours=24):
    """The lowest available memory on `node` in the last `hours`, at every scrape (30 s: a wider step skips short
    dips), and the biggest live users at that moment: (available, when, [(name, bytes)]) or None."""
    series = query_range(f'node_memory_MemAvailable_bytes{{instance="{node}"}}', hours, 30)
    if not series or not series[0][1]:
        return None
    when, avail = min(series[0][1], key=lambda p: p[1])
    live = {(m["namespace"], m["pod"]) for m, _ in _query_at(NOW["live"], when)}
    pods = [(app_name(m["pod"]), v) for m, v in _query_at(POD_WS, when)
            if node_of(m) == node and (m["namespace"], m["pod"]) in live]
    k3s = [("k3s (Kubernetes itself)", v) for m, v in _query_at(NOW["k3s"], when) if node_of(m) == node]
    docker = sum(v for _, v in _query_at(DOCKER_WS, when)) if node == DOCKER_NODE else 0
    users = pods + k3s + ([("Docker containers", docker)] if docker else [])
    return avail, when, sorted(users, key=lambda u: -u[1])[:4]


def _query_at(expr, when):
    r = _http.get(f"{PROMETHEUS}/api/v1/query", params={"query": expr, "time": when})
    r.raise_for_status()
    return [(s["metric"], float(s["value"][1])) for s in r.json()["data"]["result"]]


# ---- from raw numbers to the page (no Prometheus here: tests feed `raw` directly) -------------------------------

def _by_node(rows):
    out = {}
    for labels, v in rows:
        n = node_of(labels)
        out[n] = out.get(n, 0) + v
    return out


def _trend(series_list, keep):
    """Sum of the series that `keep(labels)` accepts, per timestamp; max instead of sum would hide a real second
    replica, sum counts a rollout's old pod for its last few minutes (a short bump, not a wrong total)."""
    points = {}
    for labels, values in series_list:
        if keep(labels):
            for t, v in values:
                points[t] = points.get(t, 0) + v
    return [points[t] for t in sorted(points)]


def build(raw):
    """Machines (with their parts) and the table of everything using memory."""
    total, avail, free = (_by_node(raw[k]) for k in ("total", "available", "free"))
    root = _by_node(raw.get("root", []))
    swap, pods, k3s = (_by_node(raw[k]) for k in ("swap", "pods", "k3s"))
    alloc = _by_node(raw["allocatable"])
    stuck = sorted(app_name(m.get("pod", "?")) for m, _ in raw.get("unschedulable", []))
    live = {(m["namespace"], m["pod"]) for m, _ in raw["live"]}
    req = {(m["namespace"], m["pod"]): v for m, v in raw["requests"]}
    lim = {(m["namespace"], m["pod"]): v for m, v in raw["limits"]}
    trends = raw.get("trends", {})
    rows = []

    # Pods: one row per live pod; apps that ran in the last 24 h but aren't running now get one row with their peak.
    peaks = {}
    for m, v in raw["pod_peak"]:
        key = (m["namespace"], app_name(m["pod"]), node_of(m))
        peaks[key] = max(peaks.get(key, 0), v)
    seen, pod_node = set(), {}
    for m, v in raw["pod_ws"]:
        ns, pod, node = m["namespace"], m["pod"], node_of(m)
        if (ns, pod) not in live:
            continue
        app = app_name(pod)
        seen.add((ns, app, node))
        pod_node[(ns, pod)] = node
        rows.append({"name": app, "detail": f"{ns}/{pod}", "kind": "pod", "machine": node, "now": v,
                     "booked": req.get((ns, pod)), "limit": lim.get((ns, pod)),
                     "peak": max(peaks.get((ns, app, node), 0), v), "running": True,
                     "trend": _trend(trends.get("pod_ws", []), lambda l, ns=ns, app=app, node=node:
                                     l.get("namespace") == ns and app_name(l.get("pod", "")) == app
                                     and node_of(l) == node)})
    for (ns, app, node), peak in peaks.items():
        if (ns, app, node) not in seen and peak > 0:
            rows.append({"name": app, "detail": f"{ns} · not running now", "kind": "pod", "machine": node, "now": None,
                         "booked": None, "limit": None, "peak": peak, "running": False,
                         "trend": _trend(trends.get("pod_ws", []), lambda l, ns=ns, app=app, node=node:
                                         l.get("namespace") == ns and app_name(l.get("pod", "")) == app
                                         and node_of(l) == node)})

    k3s_peak = _by_node(raw["k3s_peak"])
    for node, v in k3s.items():
        rows.append({"name": "k3s", "detail": "Kubernetes itself: k3s, containerd and the container shims",
                     "kind": "k3s", "machine": node, "now": v, "booked": None, "limit": None,
                     "peak": max(k3s_peak.get(node, 0), v), "running": True,
                     "trend": _trend(trends.get("k3s", []), lambda l, node=node: node_of(l) == node)})

    docker_peak = {m.get("name"): v for m, v in raw["docker_peak"]}
    docker = 0.0
    for m, v in raw["docker"]:
        docker += v
        name = m.get("name", "?")
        rows.append({"name": name, "detail": f"Docker · {m.get('image', '')}".rstrip(" ·"), "kind": "docker",
                     "machine": DOCKER_NODE, "now": v, "booked": None, "limit": None,
                     "peak": max(docker_peak.get(name, 0), v), "running": True,
                     "trend": _trend(trends.get("docker", []), lambda l, name=name: l.get("name") == name)})

    prog_peak = {(node_of(m), m.get("groupname")): v for m, v in raw["programs_peak"]}
    programs = {}
    for m, v in raw["programs"]:
        group, node = m.get("groupname", ""), node_of(m)
        if _program_counted_elsewhere(group) or v < 1024 ** 2:
            continue
        programs[node] = programs.get(node, 0) + v
        where = group.split("|", 1)[1].strip("[]").replace("/../../../..", "") if "|" in group else ""
        rows.append({"name": m.get("program") or group.split("|")[0], "detail": f"outside containers · {where}".rstrip(" ·"),
                     "kind": "program", "machine": node, "now": v, "booked": None, "limit": None,
                     "peak": max(prog_peak.get((node, group), 0), v), "running": True, "trend": []})

    machines = []
    order = list(MACHINE_NAMES)
    for node in sorted(total, key=lambda n: (order.index(n) if n in order else len(order), n)):
        t, a, f = total[node], avail.get(node, 0), free.get(node, 0)
        used = t - a
        r = root.get(node, used)          # no kubelet numbers: fall back to the kernel's "used"
        parts = {"pods": pods.get(node, 0), "k3s": k3s.get(node, 0),
                 "docker": docker if node == DOCKER_NODE else 0}
        other = r - sum(parts.values())
        overlap = -other if other < 0 else 0
        r = max(r, sum(parts.values()))   # the Acer's root cgroup reads a little below its children: trust the parts
        parts["other"] = max(other, 0)
        parts["kernel"] = max(used - r, 0)
        parts["cache"] = max(t - f - r - parts["kernel"], 0)
        parts["free"] = f
        booked = sum(v for key, v in req.items() if pod_node.get(key) == node)
        mine = [r for r in rows if r["machine"] == node and r["now"]]
        machines.append({
            "node": node, "name": MACHINE_NAMES.get(node, node), "total": t, "available": a, "free": f,
            "used": used, "programs": programs.get(node, 0), "swap": swap.get(node, 0), "parts": parts, "overlap": overlap,
            "booked": booked, "allocatable": alloc.get(node, t),
            "top": sorted(mine, key=lambda r: -r["now"])[:3],
            "available_trend": _trend(trends.get("available", []), lambda l, node=node: node_of(l) == node),
        })
    for r in rows:
        size = total.get(r["machine"])
        r["share"] = (r["now"] / size) if size and r["now"] else None
        r["machine_name"] = MACHINE_NAMES.get(r["machine"], r["machine"])
    rows.sort(key=lambda r: (not r["running"], -(r["now"] or 0), -r["peak"]))
    return {"machines": machines, "rows": rows, "stuck": stuck, "at": raw.get("at")}


def verdict(m):
    """ok / warn / bad and one plain sentence for a machine."""
    share = m["available"] / m["total"] if m["total"] else 0
    booked = m["booked"] / m["allocatable"] if m["allocatable"] else 0
    top = m["top"][0] if m["top"] else None
    who = f"; the biggest user is {top['name']} ({top['now'] / G:.1f} GB)" if top else ""
    if share < 0.12:
        return "bad", f"Only {m['available'] / G:.1f} GB left{who}."
    if share < 0.25:
        return "warn", f"Getting low: {m['available'] / G:.1f} GB left{who}."
    note = ""
    if booked > 0.92:     # by design on the Acer (filled first, ranks decide who gives way): information, not a warning
        note = (f" Booked {booked:.0%}: a new pod gets in only by pushing out a lower-ranked one, even though memory "
                f"is free.")
    return "ok", f"Fine: {m['available'] / G:.1f} GB available{who}.{note}"


@source("memory.now", ttl=30)
def now():
    return build(fetch())


@source("memory.low", ttl=300)
def low(node):
    return low_point(node)
