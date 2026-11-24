"""One app's page (lab.machaka.net/machines/apps/<namespace>/<app>): Grafana's "Kubernetes / Views / Pods" for one
app, with the Apps page's row on top: its memory against its booking and limit, its CPU against what it asked for,
its network, its restarts, over 24 hours or 7 days; and the last lines of its log (the lab may read logs in apps).

An app is every pod whose name is the app's plus Kubernetes' suffix (memory.app_name), summed: a DaemonSet's line is
both machines together, a job's has a bump per run.
"""
import concurrent.futures
import re
import time

from . import k8s, prom, source
from .memory import G, app_name

RANGES = {"24h": (24, 300, "5m"), "7d": (168, 1800, "30m")}
LOG_NAMESPACES = {"apps"}          # the lab's Role reads pod logs only there (k8s/tools/lab.yaml)


SAFE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")       # Kubernetes names: nothing to escape in PromQL


def queries(ns, app, w):
    if not (SAFE.match(ns) and SAFE.match(app)):
        return {}
    sel = f'namespace="{ns}",pod=~"{app}(-.+)?"'
    c = f'job="kubelet-cadvisor",{sel},container!="",container!="POD"'
    return {
        "mem": f"sum by (pod) (max_over_time(container_memory_working_set_bytes{{{c}}}[{w}]))",
        "booked": f'sum by (pod) (kube_pod_container_resource_requests{{resource="memory",{sel}}})',
        "limit": f'sum by (pod) (kube_pod_container_resource_limits{{resource="memory",{sel}}})',
        "cpu": f"sum by (pod) (rate(container_cpu_usage_seconds_total{{{c}}}[{w}]))",
        "cpu_req": f'sum by (pod) (kube_pod_container_resource_requests{{resource="cpu",{sel}}})',
        "rx": f'sum by (pod) (rate(container_network_receive_bytes_total{{job="kubelet-cadvisor",{sel}}}[{w}]))',
        "tx": f'sum by (pod) (rate(container_network_transmit_bytes_total{{job="kubelet-cadvisor",{sel}}}[{w}]))',
        "restarts": f"sum by (pod) (increase(kube_pod_container_status_restarts_total{{{sel}}}[{w}]))",
    }


def fetch(ns, app, range_key):
    hours, step, w = RANGES[range_key]
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {k: pool.submit(prom.query_range, e, hours, step) for k, e in queries(ns, app, w).items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception:              # noqa: BLE001 - one missing graph
                out[k] = []
    out["at"] = time.time()
    return out


def summed(rows, app, scale=1.0):
    """The app's pods' series added up per time (other apps sharing the prefix left out)."""
    acc = {}
    for labels, points in rows:
        if app_name(labels.get("pod", "")) != app:
            continue
        for t, v in points:
            acc[t] = acc.get(t, 0) + v * scale
    return sorted(acc.items())


def graphs(raw, app):
    line = lambda name, slot, pts: {"name": name, "slot": slot, "points": pts}
    mem = [line("Really used", 1, summed(raw.get("mem", []), app, 1 / G)),
           line("Booked", 2, summed(raw.get("booked", []), app, 1 / G)),
           line("Limit", 8, summed(raw.get("limit", []), app, 1 / G))]
    cpu = [line("Used", 1, summed(raw.get("cpu", []), app)), line("Asked for", 2, summed(raw.get("cpu_req", []), app))]
    net = [line("In", 1, summed(raw.get("rx", []), app, 1 / 1024 ** 2)),
           line("Out", 2, summed(raw.get("tx", []), app, 1 / 1024 ** 2))]
    restarts = summed(raw.get("restarts", []), app)
    return {"mem": [s for s in mem if s["points"]], "cpu": [s for s in cpu if s["points"]],
            "net": [s for s in net if s["points"] and max(v for _, v in s["points"]) > 0],
            "restarts": [line("Restarts", 8, restarts)] if sum(v for _, v in restarts) >= 0.5 else []}


@source("appdetail", ttl=60)
def read(ns, app, range_key):
    return fetch(ns, app, range_key)


def log_of(ns, pods):
    """The last lines of the newest live pod's log, or None where the lab may not read logs."""
    if ns not in LOG_NAMESPACES or not pods:
        return None
    r = k8s.log_tail.get(ns, pods[-1], 80)
    return r.data if r.ok else None
