"""Prometheus (machines, GPU, Frigate) and Alertmanager (what's firing)."""
import time

import httpx

from . import source
from ..config import ALERTMANAGER, PROMETHEUS

_http = httpx.Client(timeout=10)


def query(expr):
    """Instant query -> [(labels, value)]."""
    r = _http.get(f"{PROMETHEUS}/api/v1/query", params={"query": expr})
    r.raise_for_status()
    return [(s["metric"], float(s["value"][1])) for s in r.json()["data"]["result"]]


def query_range(expr, hours=24, step=600):
    """Range query -> [(labels, [(ts, value), ...])]."""
    end = time.time()
    r = _http.get(f"{PROMETHEUS}/api/v1/query_range",
                  params={"query": expr, "start": end - hours * 3600, "end": end, "step": step})
    r.raise_for_status()
    return [(s["metric"], [(float(t), float(v)) for t, v in s["values"]]) for s in r.json()["data"]["result"]]


def by_instance(expr, label="instance"):
    return {m.get(label, ""): v for m, v in query(expr)}


# node-exporter's instance label is the node name (relabelled in prometheus.yml): moudimash99, acer-laptop.
MACHINE_QUERIES = {
    "cpu": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
    "mem": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "mem_total": "node_memory_MemTotal_bytes",
    "disk": '100 * max by (instance) (1 - node_filesystem_avail_bytes{fstype=~"ext4|xfs|btrfs"}'
            ' / node_filesystem_size_bytes{fstype=~"ext4|xfs|btrfs"})',
    "temp": "max by (instance) (node_hwmon_temp_celsius)",
    "load": "node_load5",
    "cores": 'count by (instance) (node_cpu_seconds_total{mode="idle"})',
    "uptime": "time() - node_boot_time_seconds",
}


@source("prom.machines", ttl=30)
def machines():
    """Per node name: cpu %, mem %, disk % (fullest), hottest °C, load, cores, uptime."""
    out = {}
    for key, expr in MACHINE_QUERIES.items():
        for inst, v in by_instance(expr).items():
            out.setdefault(inst, {})[key] = v
    return out


GPU = {
    "util": "nvidia_smi_utilization_gpu_ratio * 100",
    "mem_used": "nvidia_smi_memory_used_bytes",
    "mem_total": "nvidia_smi_memory_total_bytes",
    "temp": "nvidia_smi_temperature_gpu",
    "power": "nvidia_smi_power_draw_watts",
    "fan": "nvidia_smi_fan_speed_ratio * 100",
    "clock": "nvidia_smi_clocks_current_graphics_clock_hz",
}


@source("prom.gpu", ttl=30)
def gpu():
    out = {}
    for key, expr in GPU.items():
        res = query(expr)
        if res:
            out[key] = res[0][1]
            out.setdefault("name", res[0][0].get("name", ""))
    return out


@source("prom.gpu_history", ttl=300)
def gpu_history(hours=24):
    return {"util": (query_range(GPU["util"], hours) or [({}, [])])[0][1],
            "temp": (query_range(GPU["temp"], hours) or [({}, [])])[0][1],
            "mem": (query_range(GPU["mem_used"] + " / 1e9", hours) or [({}, [])])[0][1]}


@source("prom.frigate", ttl=60)
def frigate():
    out = {}
    for m, v in query('{__name__=~"frigate_camera_fps|frigate_detection_fps|frigate_detector_inference_speed_seconds"}'):
        out[m["__name__"]] = out.get(m["__name__"], 0) + v
    return out


@source("alerts", ttl=30)
def alerts():
    """Firing, unsilenced alerts, newest first."""
    r = _http.get(f"{ALERTMANAGER}/api/v2/alerts", params={"active": "true", "silenced": "false", "inhibited": "false"})
    r.raise_for_status()
    out = []
    for a in r.json():
        lab, ann = a.get("labels", {}), a.get("annotations", {})
        out.append({"name": lab.get("alertname", "?"), "severity": lab.get("severity", "warning"),
                    "summary": ann.get("summary") or ann.get("description") or "", "since": a.get("startsAt"),
                    "labels": lab})
    return sorted(out, key=lambda a: a["since"] or "", reverse=True)
