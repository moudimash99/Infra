"""The Apps page against saved Prometheus answers: pods grouped into apps (one row per machine for a DaemonSet),
each app's state and words (can't start, waiting for room, restarted, finished), the 24 hours hour by hour, the
machines' room, restarts grouped by machine reboot, the ranks, and the page rendering.
Run: PYTHONPATH=. python -m pytest -q tests/test_apps.py (from lab/)."""
import time

from app.sources import Reading, apps, memory, overview
from tests.test_machines import G, raw_fixture, s
from tests.test_status import CATALOG, NOW, raw as overview_raw

H = 3600
RANKS = [{"name": "homelab-critical", "value": 1000000, "pushes": True, "default": False, "what": "Keep running."},
         {"name": "batch-high", "value": 30000, "pushes": False, "default": False, "what": "Jobs."}]
CAT = {**CATALOG, "ranks": RANKS, "notes": {"frigate": "Cat camera recorder", "node-exporter": "Machine numbers"}}


def info(ns, pod, node, kind="ReplicaSet", rank="homelab-critical"):
    return s(1 / G, namespace=ns, pod=pod, node=node, created_by_kind=kind, priority_class=rank)


def one(ns, pod, **labels):
    return s(1 / G, namespace=ns, pod=pod, **labels)


def apps_raw():
    acer, srv = "acer-laptop", "moudimash99"
    return {
        "info": [info("apps", "frigate-64b885b9c7-rf2bv", acer),
                 info("monitoring", "node-exporter-k22dr", srv, "DaemonSet"),
                 info("monitoring", "node-exporter-abcde", acer, "DaemonSet"),
                 info("apps", "fm-webllm-7db466b596-x647r", "", rank="batch-high"),
                 info("apps", "cat-flash-29924600-gc777", acer, "Job", "batch-high"),
                 info("apps", "broken-5d9f7c6b8d-zzzzz", srv)],
        "phase": [one("apps", "frigate-64b885b9c7-rf2bv", phase="Running"),
                  one("monitoring", "node-exporter-k22dr", phase="Running"),
                  one("monitoring", "node-exporter-abcde", phase="Running"),
                  one("apps", "fm-webllm-7db466b596-x647r", phase="Pending"),
                  one("apps", "cat-flash-29924600-gc777", phase="Succeeded"),
                  one("apps", "broken-5d9f7c6b8d-zzzzz", phase="Running")],
        "ready": [one("apps", "frigate-64b885b9c7-rf2bv"), one("monitoring", "node-exporter-k22dr"),
                  one("monitoring", "node-exporter-abcde")],
        "unschedulable": [one("apps", "fm-webllm-7db466b596-x647r")],
        "created": [s((NOW - 600) / G, namespace="apps", pod="fm-webllm-7db466b596-x647r")],
        "start": [s((NOW - 4 * 86400) / G, namespace="apps", pod="frigate-64b885b9c7-rf2bv"),
                  s((NOW - 30 * 86400) / G, namespace="monitoring", pod="node-exporter-k22dr")],
        "cstart": [s((NOW - 9 * H) / G, namespace="monitoring", pod="node-exporter-k22dr")],   # restarted with the server
        "restarts_24h": [s(1 / G, namespace="monitoring", pod="node-exporter-k22dr"),
                         s(5 / G, namespace="apps", pod="broken-5d9f7c6b8d-zzzzz")],
        "last_reason": [one("monitoring", "node-exporter-k22dr", container="node-exporter", reason="Unknown"),
                        one("apps", "broken-5d9f7c6b8d-zzzzz", container="app", reason="Error")],
        "last_code": [s(1 / G, namespace="apps", pod="broken-5d9f7c6b8d-zzzzz", container="app")],
        "last_when": [s((NOW - 9 * H) / G, namespace="monitoring", pod="node-exporter-k22dr", container="node-exporter"),
                      s((NOW - 60) / G, namespace="apps", pod="broken-5d9f7c6b8d-zzzzz", container="app")],
        "waiting": [one("apps", "broken-5d9f7c6b8d-zzzzz", container="app", reason="CrashLoopBackOff")],
        "alloc": [s(6.6, node=acer), s(7.4, node=srv)], "booked": [s(6.1, node=acer), s(4.6, node=srv)],
        "used": [s(4.5, node=acer), s(2.9, node=srv)], "used_peak": [s(5.3, node=acer), s(4.6, node=srv)],
        "wanted": [s(0.5)],
        "suspended": [one("apps", "", cronjob="cat-flash")],
        "strip": {"running": [({"namespace": "apps", "pod": "frigate-64b885b9c7-rf2bv"},
                               [(NOW - k * 300, 1.0) for k in range(288)]),
                              ({"namespace": "apps", "pod": "cat-flash-29924600-gc777"}, [(NOW - 5 * H, 1.0)])],
                  "waiting": [({"namespace": "apps", "pod": "fm-webllm-7db466b596-x647r"}, [(NOW - 300, 1.0)])],
                  "failed": []},
        "r": {"alloc": [({"node": acer}, [(NOW - H, 6.6 * G), (NOW, 6.6 * G)])],
              "booked": [({"node": acer}, [(NOW - H, 6.0 * G), (NOW, 6.1 * G)])],
              "used": [({"node": acer}, [(NOW - H, 4.4 * G), (NOW, 4.5 * G)])]},
        "at": NOW,
    }


def test_apps_rows_and_states():
    d = apps.build(apps_raw(), memory.build(raw_fixture()), CAT, now=NOW)
    rows = {(r["name"], r["node"]): r for r in d["rows"]}
    assert rows[("broken", "moudimash99")]["state"] == "bad"
    assert rows[("broken", "moudimash99")]["text"] == "can't start: keeps crashing"
    assert rows[("fm-webllm", "")]["text"] == "waiting 10 min for room" and rows[("fm-webllm", "")]["state"] == "warn"
    assert ("node-exporter", "moudimash99") in rows and ("node-exporter", "acer-laptop") in rows   # one per machine
    ne = rows[("node-exporter", "moudimash99")]
    assert ne["text"] == "up 9 h" and ne["stop_words"] == "stopped by a machine restart"
    frigate = rows[("frigate", "acer-laptop")]
    assert frigate["state"] == "ok" and frigate["what"] == "Cat camera recorder" and frigate["rank_value"] == 1000000
    assert frigate["mem"] == 1.2 * G and frigate["booked"] == 1.5 * G
    assert all(c["state"] == "running" for c in frigate["cells"])
    flash = rows[("cat-flash", "acer-laptop")]
    assert flash["state"] == "idle" and flash["text"] == "paused (turned off on purpose)" and flash["is_job"]
    assert sum(1 for c in flash["cells"] if c["state"] == "running") == 1
    assert [r["name"] for r in d["rows"]][:2] == ["broken", "fm-webllm"]                 # the worst first


def test_room_restarts_and_ranks():
    d = apps.build(apps_raw(), memory.build(raw_fixture()), CAT, now=NOW)
    acer = next(m for m in d["machines"] if m["node"] == "acer-laptop")
    assert round(acer["booked_share"] * 100) == 92 and acer["name"] == "Acer"
    assert [s["name"] for s in d["room_graphs"]["acer-laptop"]] == ["Room for pods", "Booked", "Really used"]
    assert d["wanted"] == 0.5 * G
    assert [b["name"] for b in d["reboots"]] == ["Server"] and d["reboots"][0]["apps"] == ["node-exporter"]
    assert [r["name"] for r in d["restarted"]] == ["broken"]                              # the reboot isn't listed alone
    assert "crashed (exit code 1)" == d["restarted"][0]["stop_words"]
    held = {k["name"]: k["apps"] for k in d["ladder"]}
    assert held["homelab-critical"] == 4 and held["batch-high"] == 1


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_apps_page(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_status
    from app.sources import prom
    monkeypatch.setattr(apps, "read", Fixed(apps_raw()))
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw_fixture())))
    monkeypatch.setattr(overview, "now", Fixed(overview.build(overview_raw(), CATALOG, now=NOW)))
    monkeypatch.setattr(overview, "load_catalog", lambda: CAT)
    monkeypatch.setattr(prom, "alerts", Fixed([]))
    client = TestClient(site_status.app, root_path="/machines")
    for path in ("/apps", "/apps?range=7d", "/apps?range=x"):
        r = client.get(path)
        assert r.status_code == 200 and "Traceback" not in r.text, path
    text = client.get("/apps").text
    for words in ("Is there room?", "Every app", "Who's waiting for room?", "What restarted today?", "The ranks",
                  "keeps crashing", "The Server restarted", "may push lower ranks out"):
        assert words in text, words
    assert text.count('class="h ') >= 24 * 5 and 'data-sort-for="app-list"' in text
    assert '<button type="button" data-v="pc">' not in text                    # the PC runs no apps


def test_app_page(monkeypatch):
    """One app: its row, its graphs (pods of other apps sharing the prefix left out), its log."""
    from starlette.testclient import TestClient
    from app import site_status
    from app.sources import appdetail, k8s, prom
    pts = lambda *v: [(NOW - (len(v) - i) * 300, x) for i, x in enumerate(v)]
    detail = {"mem": [({"pod": "frigate-64b885b9c7-rf2bv"}, pts(1.2 * G, 1.3 * G)),
                      ({"pod": "frigate-helper-5d9f7c6b8d-zzzzz"}, pts(5 * G, 5 * G))],      # another app
              "booked": [({"pod": "frigate-64b885b9c7-rf2bv"}, pts(1.5 * G, 1.5 * G))],
              "cpu": [({"pod": "frigate-64b885b9c7-rf2bv"}, pts(0.2, 0.3))], "at": NOW}
    g = appdetail.graphs(detail, "frigate")
    assert [s["name"] for s in g["mem"]] == ["Really used", "Booked"] and g["mem"][0]["points"][-1][1] == 1.3
    monkeypatch.setattr(apps, "read", Fixed(apps_raw()))
    monkeypatch.setattr(appdetail, "read", Fixed(detail))
    monkeypatch.setattr(k8s, "log_tail", Fixed("ready: recording camera living_room"))
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw_fixture())))
    monkeypatch.setattr(overview, "load_catalog", lambda: CAT)
    monkeypatch.setattr(prom, "alerts", Fixed([]))
    client = TestClient(site_status.app, root_path="/machines")
    text = client.get("/apps/apps/frigate").text
    for words in ("Cat camera recorder", "Right now", "Memory", "CPU", "Restarts", "recording camera living_room",
                  "may push lower ranks out"):
        assert words in text, words
    other = client.get("/apps/monitoring/node-exporter?range=7d")
    assert other.status_code == 200 and "may read logs only in apps" in other.text
    assert 'href="/machines/apps/apps/frigate"' in client.get("/apps").text
