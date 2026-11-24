"""One machine's page (lab.machaka.net/machines/server, /acer): Grafana's Node Exporter Full, Node temperatures,
"What is using the machines", Docker containers and the GPU, merged for one machine, with where its memory goes
(memory.py).

  Right now         memory, CPU, load, fullest disk, hottest sensor, uptime, battery: each with its state
  Memory            available over time and who used it (the biggest apps over time)
  Using it          every app, k3s, Docker container and program on it: memory now, its peak, its share, CPU
  CPU               busy and waiting-for-disk over time, the biggest users over time
  Struggling?       pressure (PSI): the share of time something had to wait for memory, CPU or the disk
  Temperatures      every sensor now and at its peak; the hottest over time
  Disks             each filesystem (how full, how fast it fills), each disk's reads, writes and busy time
  Network           each network card: in and out now and in the last day; the traffic over time
  GPU (the Acer)    busy, memory, heat, power; who is on it
  Docker (server)   the server's Docker containers

Queries run side by side; a page reads one machine over one range (24 h or 7 d) and keeps it a minute.
"""
import concurrent.futures
import time

from . import prom, source
from .overview import Projects
from .memory import DOCKER_NODE, DOCKER_WS, G, K3S_CGROUP, MACHINE_NAMES, WS, app_name

SLUGS = {"server": "moudimash99", "acer": "acer-laptop"}
NODE_SLUG = {node: slug for slug, node in SLUGS.items()}
RANGES = {"24h": (24, 300, "5m"), "7d": (168, 1800, "30m")}   # hours, step (s), window for rates and peaks
FS = 'fstype=~"ext4|xfs|btrfs"'
NET = 'device=~"(en|eth|wl|ww).*"'                              # the machine's own network cards, not the virtual ones
DISK = 'device=~"nvme[0-9]+n[0-9]+|sd[a-z]+|vd[a-z]+|mmcblk[0-9]+"'   # whole disks (not partitions or LVM on top)
CONTAINERS = 'container!="",container!="POD"'
POD_SEL = f'job="kubelet-cadvisor",{CONTAINERS}'
GPU_POD = ('sum by (pod) (gpu_process_sm_percent * on (pod_uid) group_left (pod) '
           'label_replace(kube_pod_info, "pod_uid", "$1", "uid", "(.+)"))')
GPU_POD_MEM = ('sum by (pod) (gpu_process_memory_bytes * on (pod_uid) group_left (pod) '
               'label_replace(kube_pod_info, "pod_uid", "$1", "uid", "(.+)"))')
TOP = 5             # lines per graph (the chart colours run s1..s5)

# Sensor chips in plain words (node_hwmon_chip_names gives the driver: coretemp, nvme, acpitz, ...).
CHIP_WORDS = {"coretemp": "CPU", "nvme": "SSD", "acpitz": "Motherboard", "dell_smm": "Dell board sensor",
              "iwlwifi_1": "Wi-Fi card", "ath10k_hwmon": "Wi-Fi card", "pch_cannonlake": "Chipset",
              "pch_skylake": "Chipset"}


def instant(node, docker, gpu):
    """The instant queries for one machine."""
    i = f'instance="{node}"'
    q = {
        "cpu": f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{i}}}[5m])))',
        "iowait": f'100 * avg(rate(node_cpu_seconds_total{{mode="iowait",{i}}}[5m]))',
        "cores": f'count(node_cpu_seconds_total{{mode="idle",{i}}})',
        "load": f'{{__name__=~"node_load1|node_load5|node_load15",{i}}}',
        "uptime": f'time() - node_boot_time_seconds{{{i}}}',
        "up": f'up{{job="node-exporter",{i}}}',
        "fs_size": f'node_filesystem_size_bytes{{{i},{FS}}}',
        "fs_avail": f'node_filesystem_avail_bytes{{{i},{FS}}}',
        "fs_deriv": f'deriv(node_filesystem_avail_bytes{{{i},{FS}}}[24h])',
        "fs_ro": f'node_filesystem_readonly{{{i},{FS}}}',
        "temps": f'node_hwmon_temp_celsius{{{i}}}',
        "temp_peak": f'max_over_time(node_hwmon_temp_celsius{{{i}}}[24h])',
        "temp_crit": f'node_hwmon_temp_crit_celsius{{{i}}}',
        "sensor_label": f'node_hwmon_sensor_label{{{i}}}',
        "chip_names": f'node_hwmon_chip_names{{{i}}}',
        "fans": f'node_hwmon_fan_rpm{{{i}}}',
        "battery": f'node_power_supply_capacity{{{i}}}',
        "power_online": f'node_power_supply_online{{{i}}}',
        "power_info": f'node_power_supply_info{{{i}}}',
        "psi_cpu": f'100 * rate(node_pressure_cpu_waiting_seconds_total{{{i}}}[5m])',
        "psi_mem": f'100 * rate(node_pressure_memory_waiting_seconds_total{{{i}}}[5m])',
        "psi_io": f'100 * rate(node_pressure_io_waiting_seconds_total{{{i}}}[5m])',
        "psi_mem_24h": f'100 * rate(node_pressure_memory_waiting_seconds_total{{{i}}}[24h])',
        "net_rx": f'rate(node_network_receive_bytes_total{{{i},{NET}}}[5m])',
        "net_tx": f'rate(node_network_transmit_bytes_total{{{i},{NET}}}[5m])',
        "net_rx_24h": f'increase(node_network_receive_bytes_total{{{i},{NET}}}[24h])',
        "net_tx_24h": f'increase(node_network_transmit_bytes_total{{{i},{NET}}}[24h])',
        "net_err_24h": (f'increase(node_network_receive_errs_total{{{i},{NET}}}[24h]) + '
                        f'increase(node_network_transmit_errs_total{{{i},{NET}}}[24h])'),
        "net_drop_24h": (f'increase(node_network_receive_drop_total{{{i},{NET}}}[24h]) + '
                         f'increase(node_network_transmit_drop_total{{{i},{NET}}}[24h])'),
        "net_up": f'node_network_up{{{i},{NET}}}',
        "disk_read": f'rate(node_disk_read_bytes_total{{{i},{DISK}}}[5m])',
        "disk_write": f'rate(node_disk_written_bytes_total{{{i},{DISK}}}[5m])',
        "disk_busy": f'100 * rate(node_disk_io_time_seconds_total{{{i},{DISK}}}[5m])',
        "disk_read_24h": f'increase(node_disk_read_bytes_total{{{i},{DISK}}}[24h])',
        "disk_write_24h": f'increase(node_disk_written_bytes_total{{{i},{DISK}}}[24h])',
        "pod_cpu": f'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{{{POD_SEL},node="{node}"}}[5m]))',
        "pod_cpu_peak": (f'max_over_time(sum by (namespace, pod) (rate(container_cpu_usage_seconds_total'
                         f'{{{POD_SEL},node="{node}"}}[5m]))[24h:5m])'),
        "k3s_cpu": f'sum(rate(container_cpu_usage_seconds_total{{job="kubelet-cadvisor",node="{node}",id=~"{K3S_CGROUP}"}}[5m]))',
        "cpu_req": f'sum by (namespace, pod) (kube_pod_container_resource_requests{{resource="cpu",node="{node}"}})',
    }
    if docker:
        q["docker_cpu"] = 'sum by (name) (rate(container_cpu_usage_seconds_total{job="cadvisor-docker",name!=""}[5m]))'
        q["docker_start"] = 'max by (name) (container_start_time_seconds{job="cadvisor-docker",name!=""})'
        q["docker_image"] = 'max by (name, image) (container_last_seen{job="cadvisor-docker",name!=""})'
    if gpu:
        q.update({"gpu_util": "nvidia_smi_utilization_gpu_ratio * 100", "gpu_mem": "nvidia_smi_memory_used_bytes",
                  "gpu_mem_total": "nvidia_smi_memory_total_bytes", "gpu_temp": "nvidia_smi_temperature_gpu",
                  "gpu_power": "nvidia_smi_power_draw_watts", "gpu_power_max": "nvidia_smi_enforced_power_limit_watts",
                  "gpu_fan": "nvidia_smi_fan_speed_ratio * 100", "gpu_name": "nvidia_smi_gpu_info",
                  "gpu_pods": GPU_POD, "gpu_pods_mem": GPU_POD_MEM,
                  "gpu_util_24h": "avg_over_time((nvidia_smi_utilization_gpu_ratio * 100)[24h:5m])",
                  "gpu_temp_peak": "max_over_time(nvidia_smi_temperature_gpu[24h])"})
    return q


def ranged(node, w, docker, gpu):
    """The range queries for one machine; w is the window of each point (5 m over a day, 30 m over a week): peaks
    within it for memory (a short dip or spike between two points still shows), averages for CPU."""
    i = f'instance="{node}"'
    q = {
        "available": f'min_over_time(node_memory_MemAvailable_bytes{{{i}}}[{w}])',
        "pods_mem": f'max by (node) (max_over_time({WS},node="{node}",id="/kubepods.slice"}}[{w}]))',
        "app_mem": f'sum by (namespace, pod) (max_over_time({WS},{CONTAINERS},node="{node}"}}[{w}]))',
        "k3s_mem": f'max by (node) (max_over_time({WS},node="{node}",id=~"{K3S_CGROUP}"}}[{w}]))',
        "cpu": f'100 * (1 - avg(rate(node_cpu_seconds_total{{mode="idle",{i}}}[{w}])))',
        "iowait": f'100 * avg(rate(node_cpu_seconds_total{{mode="iowait",{i}}}[{w}]))',
        "app_cpu": f'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{{{POD_SEL},node="{node}"}}[{w}]))',
        "k3s_cpu": f'sum(rate(container_cpu_usage_seconds_total{{job="kubelet-cadvisor",node="{node}",id=~"{K3S_CGROUP}"}}[{w}]))',
        "psi_cpu": f'100 * rate(node_pressure_cpu_waiting_seconds_total{{{i}}}[{w}])',
        "psi_mem": f'100 * rate(node_pressure_memory_waiting_seconds_total{{{i}}}[{w}])',
        "psi_io": f'100 * rate(node_pressure_io_waiting_seconds_total{{{i}}}[{w}])',
        "temp_chip": f'max by (chip) (max_over_time(node_hwmon_temp_celsius{{{i}}}[{w}]))',
        "disk_read": f'sum(rate(node_disk_read_bytes_total{{{i},{DISK}}}[{w}]))',
        "disk_write": f'sum(rate(node_disk_written_bytes_total{{{i},{DISK}}}[{w}]))',
        "disk_busy": f'max(100 * rate(node_disk_io_time_seconds_total{{{i},{DISK}}}[{w}]))',
        "net_rx": f'sum(rate(node_network_receive_bytes_total{{{i},{NET}}}[{w}]))',
        "net_tx": f'sum(rate(node_network_transmit_bytes_total{{{i},{NET}}}[{w}]))',
    }
    if docker:
        q["docker_mem"] = f'max by (name) (max_over_time({DOCKER_WS}[{w}]))'
        q["docker_cpu"] = f'sum by (name) (rate(container_cpu_usage_seconds_total{{job="cadvisor-docker",name!=""}}[{w}]))'
    if gpu:
        q.update({"gpu_util": f"avg_over_time((nvidia_smi_utilization_gpu_ratio * 100)[{w}:1m])",
                  "gpu_mem": f"max_over_time(nvidia_smi_memory_used_bytes[{w}])",
                  "gpu_temp": f"max_over_time(nvidia_smi_temperature_gpu[{w}])",
                  "gpu_pods": GPU_POD})
    return q


def fetch(node, range_key):
    hours, step, w = RANGES[range_key]
    docker, gpu = node == DOCKER_NODE, node == "acer-laptop"
    now_q, range_q = instant(node, docker, gpu), ranged(node, w, docker, gpu)
    raw, errors = {"r": {}}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {("i", k): pool.submit(prom.query, e) for k, e in now_q.items()}
        futs.update({("r", k): pool.submit(prom.query_range, e, hours, step) for k, e in range_q.items()})
        for (kind, key), fut in futs.items():
            try:
                res = fut.result()
            except Exception as e:          # noqa: BLE001 - one missing exporter leaves one gap
                res = []
                errors.append(e)
            (raw["r"] if kind == "r" else raw)[key] = res
    if len(errors) > len(futs) // 2:
        raise errors[0]
    raw["at"] = time.time()
    return raw


# ---- from numbers to the page (tests feed `raw` directly) --------------------------------------------------------

def _one(rows, default=None):
    return rows[0][1] if rows else default


def _sum(rows):
    return sum(v for _, v in rows) if rows else None


def _gb(points):
    return [(t, v / G) for t, v in points]


def series_by(rows, key, scale=1.0):
    """[(labels, points)] -> {key(labels): points (scaled)}, summing series that share a key (an app's pods)."""
    out = {}
    for labels, points in rows:
        k = key(labels)
        if k is None:
            continue
        acc = out.setdefault(k, {})
        for t, v in points:
            acc[t] = acc.get(t, 0) + v * scale
    return {k: sorted(acc.items()) for k, acc in out.items()}


def top_lines(named, most=TOP, at_least=0.0, by="area"):
    """The `most` biggest series as chart series, biggest first: by area, the most used over the whole range (a
    container that spiked for ten minutes doesn't push out an app that used a lot all day), or by peak."""
    score = (lambda pts: sum(v for _, v in pts)) if by == "area" else (lambda pts: max(v for _, v in pts))
    ranked = sorted(((score(pts), max(v for _, v in pts), name, pts) for name, pts in named.items() if pts),
                    key=lambda r: (-r[0], r[2]))
    ranked = [r for r in ranked if r[1] > at_least][:most]
    return [{"name": name, "slot": i + 1, "points": pts} for i, (_, _, name, pts) in enumerate(ranked)]


def line(name, points, slot=1, scale=1.0):
    return {"name": name, "slot": slot, "points": [(t, v * scale) for t, v in points]}


def chip_word(chip, chip_name):
    """A sensor chip in plain words; a thermal zone the machine no longer names is "Other sensor"."""
    if chip_name in CHIP_WORDS:
        return CHIP_WORDS[chip_name]
    if chip_name:
        return chip_name
    return "Other sensor" if (chip or "").startswith("thermal_") else (chip or "?")


def sensor_rows(raw):
    chips = {m.get("chip"): m.get("chip_name", "") for m, _ in raw.get("chip_names", [])}
    labels = {(m.get("chip"), m.get("sensor")): m.get("label", "") for m, _ in raw.get("sensor_label", [])}
    peak = {(m.get("chip"), m.get("sensor")): v for m, v in raw.get("temp_peak", [])}
    crit = {(m.get("chip"), m.get("sensor")): v for m, v in raw.get("temp_crit", [])}
    rows = {}
    for m, v in raw.get("temps", []):
        key = (m.get("chip"), m.get("sensor"))
        word = chip_word(key[0], chips.get(key[0], ""))
        label = labels.get(key, "")
        if word == "CPU" and label:
            name = "CPU package" if label.startswith("Package") else f"CPU {label.lower()}"
        elif word == "SSD":
            name = "SSD" if label in ("", "Composite") else f"SSD {label.lower()}"
        elif word == "Dell board sensor":
            name = f"{word} {key[1].replace('temp', '')}"
        else:
            name = word
        c = crit.get(key)
        state = "bad" if v > 97 else "warn" if v > 90 else "ok"     # the alert rules (NodeHot 90, NodeVeryHot 97)
        row = {"name": name, "now": v, "peak": max(peak.get(key, v), v), "crit": c if c and c < 200 else None,
               "state": state, "chip": key[0]}
        if name not in rows or v > rows[name]["now"]:              # a thermal zone reports the same value twice
            rows[name] = row
    order = {"CPU package": 0}
    return sorted(rows.values(), key=lambda r: (order.get(r["name"], 1), not r["name"].startswith("CPU"), r["name"]))


def disk_rows(raw):
    avail = {m.get("mountpoint"): v for m, v in raw.get("fs_avail", [])}
    deriv = {m.get("mountpoint"): v for m, v in raw.get("fs_deriv", [])}
    ro = {m.get("mountpoint"): v for m, v in raw.get("fs_ro", [])}
    rows = []
    for m, size in raw.get("fs_size", []):
        mount = m.get("mountpoint", "?")
        free = avail.get(mount, 0)
        pct = 100 * (1 - free / size) if size else 0
        d = deriv.get(mount)
        days = free / -d / 86400 if d and d < -1024 else None          # filling up by more than 1 KB/s
        state = ("bad" if pct > 90 or ro.get(mount) else "warn" if pct > 80 or (days is not None and days < 7)
                 else "ok")
        rows.append({"mount": mount, "device": m.get("device", ""), "size": size, "free": free, "used": size - free,
                     "pct": pct, "days": days, "state": state, "ro": bool(ro.get(mount))})
    return sorted(rows, key=lambda r: -r["size"])


def device_rows(raw):
    r = {m.get("device"): v for m, v in raw.get("disk_read", [])}
    w = {m.get("device"): v for m, v in raw.get("disk_write", [])}
    r24 = {m.get("device"): v for m, v in raw.get("disk_read_24h", [])}
    w24 = {m.get("device"): v for m, v in raw.get("disk_write_24h", [])}
    out = []
    for m, busy in raw.get("disk_busy", []):
        dev = m.get("device")
        out.append({"device": dev, "busy": busy, "read": r.get(dev, 0), "write": w.get(dev, 0),
                    "read_24h": r24.get(dev), "write_24h": w24.get(dev),
                    "state": "warn" if busy > 80 else "ok"})
    return sorted(out, key=lambda d: d["device"])


def net_rows(raw):
    tx = {m.get("device"): v for m, v in raw.get("net_tx", [])}
    rx24 = {m.get("device"): v for m, v in raw.get("net_rx_24h", [])}
    tx24 = {m.get("device"): v for m, v in raw.get("net_tx_24h", [])}
    err = {m.get("device"): v for m, v in raw.get("net_err_24h", [])}
    drop = {m.get("device"): v for m, v in raw.get("net_drop_24h", [])}
    up = {m.get("device"): v for m, v in raw.get("net_up", [])}
    out = []
    for m, rx in raw.get("net_rx", []):
        dev = m.get("device")
        if up.get(dev) == 0 and not rx24.get(dev):
            continue                          # a card that is off and moved nothing today (a spare Ethernet port)
        out.append({"device": dev, "kind": "Wi-Fi" if dev.startswith(("wl", "ww")) else "Ethernet", "rx": rx,
                    "tx": tx.get(dev, 0), "rx_24h": rx24.get(dev), "tx_24h": tx24.get(dev),
                    "errors": err.get(dev, 0), "drops": drop.get(dev, 0),
                    "state": "warn" if (err.get(dev) or 0) > 100 else "ok"})
    return sorted(out, key=lambda d: -(d["rx_24h"] or 0))


def battery(raw):
    info = {m.get("power_supply"): m for m, _ in raw.get("power_info", [])}
    online = {m.get("power_supply"): v for m, v in raw.get("power_online", [])}
    mains = [ps for ps, m in info.items() if m.get("type") == "Mains"]
    cap = next(((m.get("power_supply"), v) for m, v in raw.get("battery", [])), None)
    if not cap:
        return None
    on_mains = any(online.get(ps) == 1 for ps in mains) if mains else None
    status = (info.get(cap[0]) or {}).get("status", "")
    state = "bad" if on_mains is False and cap[1] < 30 else "warn" if on_mains is False else "ok"
    return {"pct": cap[1], "plugged": on_mains, "status": status, "state": state}


def users(raw, mem_rows, node, P, total, cores):
    """Everything using this machine: memory.py's rows for it (pods, k3s, Docker, programs), with CPU added."""
    cpu = {(m.get("namespace"), m.get("pod")): v for m, v in raw.get("pod_cpu", [])}
    cpu_peak = {}
    for m, v in raw.get("pod_cpu_peak", []):
        key = (m.get("namespace"), app_name(m.get("pod", "")))
        cpu_peak[key] = max(cpu_peak.get(key, 0), v)
    req = {(m.get("namespace"), m.get("pod")): v for m, v in raw.get("cpu_req", [])}
    dcpu = {m.get("name"): v for m, v in raw.get("docker_cpu", [])}
    out = []
    for r in mem_rows:
        if r["machine"] != node:
            continue
        row = {**r, "cores": None, "cpu_peak": None, "cpu_req": None, "ns": "", "pod": ""}
        if r["kind"] == "pod":
            ns, _, pod = r["detail"].partition("/") if r["running"] else (r["detail"].split(" ")[0], "", "")
            row.update(ns=ns, pod=pod if r["running"] else "", p=P.group(ns, r["name"]),
                       cores=cpu.get((ns, pod)) if r["running"] else None,
                       cpu_peak=cpu_peak.get((ns, r["name"])), cpu_req=req.get((ns, pod)))
        elif r["kind"] == "k3s":
            row.update(p="Platform", cores=_one(raw.get("k3s_cpu", [])))
        elif r["kind"] == "docker":
            row.update(p=P.group("docker", r["name"]), cores=dcpu.get(r["name"]))
        else:
            row.update(p="")
        row["cpu_share"] = row["cores"] / cores if row["cores"] is not None and cores else None
        row["mem_share"] = r["now"] / total if r["now"] and total else None
        row["state"] = ("warn" if r.get("limit") and r["now"] and r["now"] > 0.9 * r["limit"] else
                        "ok" if r["running"] else "paused")
        out.append(row)
    return out


def build(node, raw, mem, catalog=None, range_key="24h"):
    """The page for one machine. mem: memory.now's data (its machines and rows)."""
    P = Projects(catalog or {})
    R = raw.get("r", {})
    machine = next((m for m in (mem or {}).get("machines", []) if m["node"] == node), None)
    total = machine["total"] if machine else None
    cores = _one(raw.get("cores", []))
    cpu = _one(raw.get("cpu", []))
    load = {m.get("__name__", "").replace("node_", ""): v for m, v in raw.get("load", [])}
    sensors = sensor_rows(raw)
    hottest = max(sensors, key=lambda s: s["now"]) if sensors else None
    disks, devices, nets = disk_rows(raw), device_rows(raw), net_rows(raw)
    fullest = max(disks, key=lambda d: d["pct"]) if disks else None
    rows = users(raw, (mem or {}).get("rows", []), node, P, total, cores)
    avail_share = machine["available"] / total if machine and total else None
    facts = {
        "mem": {"value": f"{machine['available'] / G:.1f} GB free" if machine else "?",
                "of": f"of {total / G:.1f} GB" if total else "", "pct": 100 * (1 - avail_share) if avail_share else 0,
                "state": "bad" if avail_share is not None and avail_share < 0.12 else
                         "warn" if avail_share is not None and avail_share < 0.25 else "ok"},
        "cpu": {"value": f"{cpu:.0f}% busy" if cpu is not None else "?", "of": f"of {cores:.0f} cores" if cores else "",
                "pct": cpu or 0, "state": "warn" if (cpu or 0) > 90 else "ok"},
        "load": {"value": f"{load.get('load5', 0):.1f}", "of": f"{cores:.0f} cores" if cores else "",
                 "pct": min(100, 100 * load.get("load5", 0) / cores) if cores else 0,
                 "state": "warn" if cores and load.get("load5", 0) > 2 * cores else "ok"},
        "disk": {"value": f"{fullest['pct']:.0f}% full" if fullest else "?",
                 "of": f"{fullest['free'] / G:.0f} GB left on {fullest['mount']}" if fullest else "",
                 "pct": fullest["pct"] if fullest else 0, "state": fullest["state"] if fullest else "ok"},
        "temp": {"value": f"{hottest['now']:.0f} °C" if hottest else "?",
                 "of": hottest["name"] if hottest else "", "pct": min(100, hottest["now"]) if hottest else 0,
                 "state": hottest["state"] if hottest else "ok"},
    }
    gpu = None
    if raw.get("gpu_util"):
        pods = sorted(((app_name(m.get("pod", "?")), v) for m, v in raw.get("gpu_pods", []) if v > 0), key=lambda u: -u[1])
        pmem = {app_name(m.get("pod", "?")): v for m, v in raw.get("gpu_pods_mem", [])}
        name = next((m.get("name") for m, _ in raw.get("gpu_name", []) if m.get("name")), "")
        util = max(_one(raw["gpu_util"], 0), sum(v for _, v in pods))
        temp = _one(raw.get("gpu_temp", []))
        gpu = {"name": name or "GPU", "util": util, "mem": _one(raw.get("gpu_mem", [])),
               "mem_total": _one(raw.get("gpu_mem_total", [])), "temp": temp, "temp_peak": _one(raw.get("gpu_temp_peak", [])),
               "power": _one(raw.get("gpu_power", [])), "power_max": _one(raw.get("gpu_power_max", [])),
               "fan": _one(raw.get("gpu_fan", [])), "util_24h": _one(raw.get("gpu_util_24h", [])),
               "users": [{"name": n, "pct": v, "mem": pmem.get(n)} for n, v in pods],
               "state": "bad" if (temp or 0) > 90 else "warn" if (temp or 0) > 85 else "ok"}
    docker = None
    if node == DOCKER_NODE:
        start = {m.get("name"): v for m, v in raw.get("docker_start", [])}
        image = {m.get("name"): m.get("image", "") for m, _ in raw.get("docker_image", [])}
        docker = [{**r, "started": start.get(r["name"]), "image": image.get(r["name"], "")}
                  for r in rows if r["kind"] == "docker"]
    psi = {k: _one(raw.get(f"psi_{k}", [])) for k in ("cpu", "mem", "io")}
    struggling = {"cpu": psi["cpu"], "mem": psi["mem"], "io": psi["io"], "mem_24h": _one(raw.get("psi_mem_24h", [])),
                  "iowait": _one(raw.get("iowait", [])),
                  "state": ("bad" if (psi["mem"] or 0) > 20 or max((v or 0) for v in psi.values()) > 40 else
                            "warn" if max((v or 0) for v in psi.values()) > 10 else "ok")}

    # graphs
    apps_mem = series_by(R.get("app_mem", []), lambda l: app_name(l.get("pod", "")) or None, 1 / G)
    apps_mem.update(series_by(R.get("k3s_mem", []), lambda l: "k3s", 1 / G))
    apps_mem.update(series_by(R.get("docker_mem", []), lambda l: l.get("name") or None, 1 / G))
    apps_cpu = series_by(R.get("app_cpu", []), lambda l: app_name(l.get("pod", "")) or None)
    apps_cpu.update(series_by(R.get("k3s_cpu", []), lambda l: "k3s"))
    apps_cpu.update(series_by(R.get("docker_cpu", []), lambda l: l.get("name") or None))
    chip_names = {m.get("chip"): m.get("chip_name", "") for m, _ in raw.get("chip_names", [])}
    temps = series_by(R.get("temp_chip", []), lambda l: chip_word(l.get("chip"), chip_names.get(l.get("chip"), "")))
    if R.get("gpu_temp"):
        temps["GPU"] = R["gpu_temp"][0][1]
    first = lambda key: R.get(key)[0][1] if R.get(key) else []
    graphs = {
        "available": [line("Available", _gb(first("available")), 1)] if first("available") else [],
        "apps_mem": top_lines(apps_mem, at_least=0.01),
        "cpu": [s for s in (line("Busy", first("cpu"), 1), line("Disk wait", first("iowait"), 4)) if s["points"]],
        "apps_cpu": top_lines(apps_cpu, at_least=0.005),
        "psi": [s for s in (line("Memory", first("psi_mem"), 1), line("CPU", first("psi_cpu"), 2),
                            line("Disk", first("psi_io"), 3)) if s["points"]],
        "temps": top_lines(temps, by="peak"),
        "disk": [s for s in (line("Read", first("disk_read"), 1, 1 / 1024 ** 2),
                             line("Written", first("disk_write"), 2, 1 / 1024 ** 2)) if s["points"]],
        "net": [s for s in (line("In", first("net_rx"), 1, 1 / 1024 ** 2), line("Out", first("net_tx"), 2, 1 / 1024 ** 2))
                if s["points"]],
        "gpu": [s for s in (line("Busy", first("gpu_util"), 1),) if s["points"]],
        "gpu_pods": top_lines(series_by(R.get("gpu_pods", []), lambda l: app_name(l.get("pod", "")) or None), at_least=0.5),
        "gpu_mem": [line("GPU memory", _gb(first("gpu_mem")), 1)] if first("gpu_mem") else [],
    }
    up = _one(raw.get("up", []))
    return {"node": node, "slug": NODE_SLUG.get(node, node), "name": MACHINE_NAMES.get(node, node), "up": up,
            "machine": machine, "facts": facts, "load": load, "cores": cores, "uptime": _one(raw.get("uptime", [])),
            "battery": battery(raw), "fans": [v for _, v in raw.get("fans", [])],
            "users": rows, "sensors": sensors, "disks": disks, "devices": devices, "nets": nets, "gpu": gpu,
            "docker": docker, "struggling": struggling, "graphs": graphs, "range": range_key, "groups": P.groups,
            "at": raw.get("at")}


@source("hosts.raw", ttl=60)
def read(node, range_key):
    return fetch(node, range_key)
