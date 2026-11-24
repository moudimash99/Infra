"""A machine's page against saved Prometheus answers: the facts, sensors in plain words, disks that fill up, the
network cards, everything using the machine with its CPU, the graphs' biggest users, and the pages rendering.
Run: PYTHONPATH=. python -m pytest -q tests/test_hosts.py (from lab/)."""
import time

from app.sources import Reading, hosts, jobruns, memory, overview
from tests.test_machines import G, raw_fixture, s
from tests.test_status import CATALOG, NOW, raw as overview_raw

H = 3600


def v(x):
    return s(x / G)                    # s() multiplies by G


def acer_raw():
    i = {"instance": "acer-laptop"}
    pts = lambda *vals: [(NOW - (len(vals) - k) * 300, x) for k, x in enumerate(vals)]
    return {
        "cpu": [v(31)], "iowait": [v(2)], "cores": [v(4)], "up": [v(1)], "uptime": [v(4 * 86400)],
        "load": [s(1.1 / G, __name__="node_load1", **i), s(1.6 / G, __name__="node_load5", **i)],
        "fs_size": [s(913, mountpoint="/", device="/dev/mapper/vg-lv"), s(1.9, mountpoint="/boot", device="/dev/sdb2")],
        "fs_avail": [s(543, mountpoint="/"), s(1.6, mountpoint="/boot")],
        "fs_deriv": [s(-300e3 / G, mountpoint="/")],                       # losing 300 KB a second
        "fs_ro": [s(0, mountpoint="/"), s(0, mountpoint="/boot")],
        "temps": [s(87 / G, chip="platform_coretemp_0", sensor="temp1"), s(85 / G, chip="platform_coretemp_0", sensor="temp2"),
                  s(29.8 / G, chip="thermal_thermal_zone0", sensor="temp0"), s(29.8 / G, chip="thermal_thermal_zone0", sensor="temp1"),
                  s(40 / G, chip="thermal_thermal_zone9", sensor="temp0")],
        "temp_peak": [s(94 / G, chip="platform_coretemp_0", sensor="temp1")],
        "temp_crit": [s(100 / G, chip="platform_coretemp_0", sensor="temp1")],
        "sensor_label": [s(1 / G, chip="platform_coretemp_0", sensor="temp1", label="Package id 0"),
                         s(1 / G, chip="platform_coretemp_0", sensor="temp2", label="Core 0")],
        "chip_names": [s(1 / G, chip="platform_coretemp_0", chip_name="coretemp"),
                       s(1 / G, chip="thermal_thermal_zone0", chip_name="acpitz")],
        "battery": [s(100 / G, power_supply="BAT1")], "power_online": [s(1 / G, power_supply="ACAD")],
        "power_info": [s(1 / G, power_supply="ACAD", type="Mains"), s(1 / G, power_supply="BAT1", type="Battery", status="Full")],
        "psi_cpu": [v(1.4)], "psi_mem": [v(0.1)], "psi_io": [v(45)],
        "net_rx": [s(85e3 / G, device="wlp2s0"), s(0, device="enp3s0")], "net_tx": [s(120e3 / G, device="wlp2s0")],
        "net_rx_24h": [s(35, device="wlp2s0"), s(0, device="enp3s0")], "net_tx_24h": [s(35, device="wlp2s0")],
        "net_up": [s(1 / G, device="wlp2s0"), s(0, device="enp3s0")],
        "disk_busy": [s(3 / G, device="sdb")], "disk_read": [s(1e6 / G, device="sdb")],
        "pod_cpu": [s(0.67 / G, namespace="apps", pod="frigate-64b885b9c7-rf2bv")],
        "cpu_req": [s(0.5 / G, namespace="apps", pod="frigate-64b885b9c7-rf2bv")],
        "k3s_cpu": [v(0.09)],
        "gpu_util": [v(0)], "gpu_mem": [s(1.0)], "gpu_mem_total": [s(4.0)], "gpu_temp": [v(78)],
        "gpu_pods": [s(95 / G, pod="bg-taste-match-gpu-29924595-xc5mt")],
        "gpu_name": [s(1 / G, name="NVIDIA GeForce GTX 1050")],
        "r": {
            "available": [({}, pts(3 * G, 2 * G, 2.7 * G))],
            "app_mem": [({"namespace": "apps", "pod": "frigate-64b885b9c7-rf2bv"}, pts(1.2 * G, 1.3 * G, 1.3 * G)),
                        ({"namespace": "apps", "pod": "kind-thing-12345678-abcde"}, pts(0, 2.5 * G, 0)),  # a short spike
                        ({"namespace": "apps", "pod": "lab-7fd4dbf58c-xwm9k"}, pts(0.2 * G, 0.2 * G, 0.2 * G))],
            "cpu": [({}, pts(20, 40, 31))], "iowait": [({}, pts(1, 2, 2))],
            "temp_chip": [({"chip": "platform_coretemp_0"}, pts(80, 94, 87)), ({"chip": "thermal_thermal_zone9"}, pts(40, 41, 40))],
            "gpu_temp": [({}, pts(70, 88, 78))],
        },
        "at": NOW,
    }


def test_acer_page_data():
    d = hosts.build("acer-laptop", acer_raw(), memory.build(raw_fixture()), CATALOG)
    assert d["slug"] == "acer" and d["facts"]["cpu"]["value"] == "31% busy"
    names = [x["name"] for x in d["sensors"]]
    assert names[0] == "CPU package" and "CPU core 0" in names and "Motherboard" in names
    assert names.count("Motherboard") == 1                       # a thermal zone reports the same value twice
    assert "Other sensor" in names                               # a zone the machine no longer names
    root = next(x for x in d["disks"] if x["mount"] == "/")
    assert 15 < root["days"] < 25 and root["state"] == "ok"      # 543 GB at 300 KB/s: about 21 days
    assert [n["device"] for n in d["nets"]] == ["wlp2s0"]        # the unplugged Ethernet port is left out
    assert d["battery"] == {"pct": 100.0, "plugged": True, "status": "Full", "state": "ok"}
    assert d["struggling"]["state"] == "bad"                     # waiting for the disk 45% of the time
    frigate = next(u for u in d["users"] if u["name"] == "frigate")
    assert frigate["cores"] == 0.67 and frigate["cpu_req"] == 0.5 and frigate["ns"] == "apps"
    assert frigate["mem_share"] == 1.2 / 7.5
    assert any(u["name"] == "cat-flash" and not u["running"] for u in d["users"])     # ran at night, shown stopped
    assert d["gpu"]["util"] == 95 and d["gpu"]["users"][0]["name"] == "bg-taste-match-gpu"
    assert d["docker"] is None                                   # Docker is the server's
    biggest = [x["name"] for x in d["graphs"]["apps_mem"]]
    assert biggest[0] == "frigate"                               # used the most over the day, not the 5-minute spike
    assert "GPU" in [x["name"] for x in d["graphs"]["temps"]]


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_machine_pages_render(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_status
    from app.sources import prom
    monkeypatch.setattr(hosts, "read", Fixed(acer_raw()))
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw_fixture())))
    monkeypatch.setattr(memory, "low", Fixed((1.6 * G, NOW - 5 * H, [("frigate", 1.2 * G)])))
    monkeypatch.setattr(overview, "now", Fixed(overview.build(overview_raw(), CATALOG, now=NOW)))
    monkeypatch.setattr(jobruns, "week", Fixed(None))
    monkeypatch.setattr(prom, "alerts", Fixed([{"name": "NodeHot", "severity": "warning", "summary": "acer-laptop: running hot",
                                                 "since": None, "labels": {"instance": "acer-laptop"}}]))
    client = TestClient(site_status.app, root_path="/machines")
    for path in ("/acer", "/acer?range=7d", "/server", "/pc", "/acer?range=nonsense"):
        r = client.get(path)
        assert r.status_code == 200 and "Traceback" not in r.text, path
    text = client.get("/acer").text
    for words in ("Right now", "Where the memory goes", "What's using it", "Is it struggling?", "Temperatures",
                  "Disks", "Network", "The GPU", "Lowest in the last 24 hours"):
        assert words in text, words
    assert 'class="on" aria-current="page">Acer</a>' in text and 'href="/machines/server' in text
    assert 'href="/machines/acer#temps"' in text                 # the bell's NodeHot goes to the Acer's temperatures
    assert 'svg class="chart"' in text
    r = client.get("/memory?m=acer-laptop", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/machines/acer#memory"
    assert "Its scheduled tasks" in client.get("/pc").text
