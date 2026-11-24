"""lab.machaka.net/machines against saved Prometheus answers: the parts add up to each machine's size, a rolled-out
pod isn't counted twice, programs inside k3s aren't listed twice, and the page renders.
Run: PYTHONPATH=. python -m pytest -q tests/test_machines.py (from lab/)."""
import time

import pytest

from app.sources import Reading, memory

G = memory.G


def s(value, **labels):
    return (labels, value * G)


def raw_fixture():
    """Two machines; on the server fm-controller was just rolled out (the old pod's series still answers)."""
    srv, acer = {"node": "moudimash99"}, {"node": "acer-laptop"}
    return {
        "total": [s(7.5, instance="moudimash99"), s(7.5, instance="acer-laptop")],
        "available": [s(3.0, instance="moudimash99"), s(2.5, instance="acer-laptop")],
        "free": [s(0.5, instance="moudimash99"), s(0.2, instance="acer-laptop")],
        "swap": [s(0.4, instance="moudimash99")],
        "root": [s(4.4, **srv), s(4.0, **acer)],     # the Acer's root reads below its children (as it does live)
        "pods": [s(3.0, **srv), s(3.9, **acer)],
        "k3s": [s(1.0, id="/system.slice/k3s.service", **srv), s(0.35, id="/system.slice/k3s-agent.service", **acer)],
        "allocatable": [s(7.5, **srv), s(6.6, **acer)],
        "pod_ws": [s(1.5, namespace="apps", pod="fm-controller-799c6b96bf-dzfd4", **srv),    # replaced: not live
                   s(0.7, namespace="apps", pod="fm-controller-88b97b7f-t4xdl", **srv),
                   s(0.4, namespace="monitoring", pod="grafana-79dc6b9df6-jwjtp", **srv),
                   s(1.2, namespace="apps", pod="frigate-64b885b9c7-rf2bv", **acer)],
        "pod_peak": [s(2.9, namespace="apps", pod="fm-controller-799c6b96bf-dzfd4", **srv),
                     s(0.7, namespace="apps", pod="fm-controller-88b97b7f-t4xdl", **srv),
                     s(0.5, namespace="monitoring", pod="grafana-79dc6b9df6-jwjtp", **srv),
                     s(1.4, namespace="apps", pod="frigate-64b885b9c7-rf2bv", **acer),
                     s(1.1, namespace="apps", pod="cat-flash-29924600-gc777", **acer)],          # ran at night
        "live": [s(1, namespace="apps", pod="fm-controller-88b97b7f-t4xdl"),
                 s(1, namespace="monitoring", pod="grafana-79dc6b9df6-jwjtp"),
                 s(1, namespace="apps", pod="frigate-64b885b9c7-rf2bv")],
        "requests": [s(3.0, namespace="apps", pod="fm-controller-88b97b7f-t4xdl"),
                     s(3.0, namespace="apps", pod="fm-controller-799c6b96bf-dzfd4"),
                     s(0.25, namespace="monitoring", pod="grafana-79dc6b9df6-jwjtp"),
                     s(1.5, namespace="apps", pod="frigate-64b885b9c7-rf2bv")],
        "limits": [],
        "docker": [s(0.1, name="cloudflared", image="cloudflare/cloudflared:latest")],
        "docker_peak": [s(0.2, name="cloudflared")],
        "programs": [s(0.78, program="k3s-server", groupname="k3s-server|[/../../../../system.slice/k3s.service]", **srv),
                     s(0.05, program="process-exporte", groupname="process-exporte|[/]", **srv),
                     s(0.08, program="dockerd", groupname="dockerd|[/../../../../system.slice/docker.service]", **srv)],
        "programs_peak": [],
        "k3s_peak": [s(1.3, **srv)],
        "trends": {"pod_ws": [({"namespace": "apps", "pod": "fm-controller-88b97b7f-t4xdl", **srv},
                               [(1, 0.5 * G), (2, 0.7 * G)])],
                   "available": [({"instance": "moudimash99"}, [(1, 2.0 * G), (2, 3.0 * G)])]},
        "at": time.time(),
    }


def test_app_names():
    assert memory.app_name("fm-controller-88b97b7f-t4xdl") == "fm-controller"
    assert memory.app_name("cat-flash-29924600-gc777") == "cat-flash"
    assert memory.app_name("node-exporter-k22dr") == "node-exporter"
    assert memory.app_name("fm-browser-acer-1") == "fm-browser-acer-1"


def test_parts_add_up_to_each_machine():
    d = memory.build(raw_fixture())
    for m in d["machines"]:
        assert sum(m["parts"].values()) == pytest.approx(m["total"]), m["name"]
        assert all(v >= 0 for v in m["parts"].values()), m["name"]
    srv, acer = d["machines"]
    assert (srv["name"], acer["name"]) == ("Server", "Acer")
    assert srv["parts"]["other"] == pytest.approx(0.3 * G)          # root 4.4 - pods 3.0 - k3s 1.0 - docker 0.1
    assert acer["overlap"] == pytest.approx(0.25 * G)               # pods 3.9 + k3s 0.35 > root 4.0
    assert acer["parts"]["other"] == 0 and acer["parts"]["docker"] == 0


def test_a_rolled_out_pod_counts_once():
    d = memory.build(raw_fixture())
    fm = [r for r in d["rows"] if r["name"] == "fm-controller"]
    assert len(fm) == 1 and fm[0]["now"] == pytest.approx(0.7 * G)
    assert fm[0]["peak"] == pytest.approx(2.9 * G)                  # the old pod's peak is the app's peak
    srv = d["machines"][0]
    assert srv["booked"] == pytest.approx(3.25 * G)                 # only live pods' bookings
    assert srv["top"][0]["name"] == "k3s"


def test_finished_jobs_and_programs():
    rows = memory.build(raw_fixture())["rows"]
    flash = [r for r in rows if r["name"] == "cat-flash"]
    assert len(flash) == 1 and not flash[0]["running"] and flash[0]["now"] is None
    assert rows[-1]["name"] == "cat-flash"                          # stopped ones last
    programs = {r["name"] for r in rows if r["kind"] == "program"}
    assert programs == {"dockerd"}                                  # k3s-server is in the k3s row; [/] is the exporter


def test_verdicts():
    m = {"available": 0.5 * G, "total": 7.5 * G, "booked": 1 * G, "allocatable": 7.5 * G,
         "top": [{"name": "fm-controller", "now": 2.9 * G}]}
    assert memory.verdict(m) == ("bad", "Only 0.5 GB left; the biggest user is fm-controller (2.9 GB).")
    assert memory.verdict({**m, "available": 1.5 * G})[0] == "warn"
    state, text = memory.verdict({**m, "available": 3 * G, "booked": 6.5 * G, "allocatable": 6.6 * G})
    assert state == "ok" and "Booked 98%" in text          # the Acer is booked full on purpose: not a warning
    assert memory.verdict({**m, "available": 3 * G})[0] == "ok"


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_page_renders(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_machines
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw_fixture())))
    monkeypatch.setattr(memory, "low", Fixed((1.3 * G, time.time() - 3600, [("fm-controller", 2.9 * G)])))
    r = TestClient(site_machines.app).get("/")
    assert r.status_code == 200 and "Traceback" not in r.text
    assert 'data-site="machines"' in r.text and "Where the memory goes" in r.text
    assert "fm-controller 2.9 GB" in r.text and "Reusable cache" in r.text and 'class="alert' not in r.text
    assert r.text.count('data-name="fm-controller ') == 1


def test_stuck_pods_raise_the_alert(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_machines
    raw = raw_fixture()
    raw["unschedulable"] = [s(1, namespace="apps", pod="bg-taste-match-gpu-29924595-xc5mt")]
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw)))
    monkeypatch.setattr(memory, "low", Fixed(None))
    text = TestClient(site_machines.app).get("/").text
    assert "Waiting to start" in text and "bg-taste-match-gpu is waiting for room" in text


def test_published_under_lab_machaka_net(monkeypatch):
    """At lab.machaka.net/machines: files and icons under /machines/static (/static there is Homepage's), the other
    sites' real addresses in the menu, and no page without the Cloudflare login."""
    from starlette.testclient import TestClient
    from app import main, site_machines
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw_fixture())))
    monkeypatch.setattr(memory, "low", Fixed(None))
    text = TestClient(site_machines.app, root_path="/machines").get("/").text
    assert 'href="/machines/static/lab.css' in text and 'href="/machines/static/icons.svg' in text
    assert 'src="/machines/static/machines.js' in text and 'href="/static/' not in text
    assert 'href="https://deals.machaka.net"' in text and 'href="/deals/"' not in text
    r = TestClient(main.app, base_url="https://lab.machaka.net").get("/machines/")
    assert r.status_code == 403
