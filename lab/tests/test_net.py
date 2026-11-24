"""The Network page against saved Prometheus answers: Traefik's services as sites (names, links from the catalog,
failures and slow answers), a site the check finds down, the traffic per machine, the apps that moved the most
(apps on the host's network left out), and the page rendering.
Run: PYTHONPATH=. python -m pytest -q tests/test_net.py (from lab/)."""
import time

from app.sources import Reading, net, overview
from tests.test_machines import G, s
from tests.test_status import CATALOG, NOW

H = 3600
CAT = {**CATALOG, "services": {**CATALOG["services"], "website": {"group": "Website and lab", "url": "https://machaka.net"},
                               "unmatched": {"group": "Board games", "url": "https://unmatched.machaka.net"}},
       "objects": {**CATALOG["objects"], "apps/Deployment/website": {"service": "website"},
                   "apps/Deployment/unmatched": {"service": "unmatched"}}}
WEB, UNM, LAB = "apps-website-http@kubernetes", "apps-unmatched-http@kubernetes", "tools-lab-http@kubernetes"


def v(x, **labels):
    return s(x / G, **labels)


def net_raw():
    return {
        "req_24h": [v(2289, service=WEB), v(2800, service=UNM), v(409, service=LAB)],
        "req_1h": [v(90, service=WEB), v(100, service=UNM), v(20, service=LAB)],
        "err_24h": [v(0, service=WEB), v(60, service=UNM), v(0, service=LAB)],
        "err_1h": [v(0, service=WEB), v(10, service=UNM)],
        "refused_24h": [v(230, service=WEB)],
        "rpm": [v(1.5, service=WEB), v(2, service=UNM)],
        "fast_24h": [v(0.99, service=WEB), v(0.84, service=UNM), v(0.34, service=LAB)],
        "p95_24h": [v(0.155, service=WEB), v(1.8, service=UNM), v(5.0, service=LAB)],
        "codes_24h": [v(12000, code="200"), v(5600, code="302"), v(1900, code="400"), v(60, code="502")],
        "conns": [v(1)],
        "net_rx": [v(149e3, instance="moudimash99"), v(329e3, instance="acer-laptop")],
        "net_tx": [v(244e3, instance="moudimash99"), v(125e3, instance="acer-laptop")],
        "net_rx_24h": [v(45.7e9, instance="moudimash99"), v(37.9e9, instance="acer-laptop")],
        "net_tx_24h": [v(4.1e9, instance="moudimash99"), v(36.9e9, instance="acer-laptop")],
        "pod_rx_24h": [v(23e9, namespace="apps", pod="bg-taste-match-fetch-29923685-bphpf"),
                       v(17e9, namespace="tools", pod="beszel-agent-k49mv")],                 # an older pod, host network
        "pod_tx_24h": [v(0.2e9, namespace="apps", pod="bg-taste-match-fetch-29923685-bphpf")],
        "hostnet": [v(1, namespace="tools", pod="beszel-agent-zzlwr")],
        "checks": [{"name": "machaka.net", "url": "https://machaka.net", "up": True, "code": 200, "ms": 45},
                   {"name": "Unmatched", "url": "https://unmatched.machaka.net", "up": False, "code": 502, "ms": None},
                   {"name": "FreeMotion", "url": "https://freemotion.machaka.net", "up": None, "code": None, "ms": None}],
        "r": {"rpm": [({"service": WEB}, [(NOW - H, 1.0), (NOW, 1.5)]), ({"service": UNM}, [(NOW - H, 2.0), (NOW, 2.0)])],
              "err": [({"service": UNM}, [(NOW - H, 0.0), (NOW, 0.2)])],
              "slow": [({"service": UNM}, [(NOW - k * 300, 0.2) for k in range(287, -1, -1)]),
                       ({"service": LAB}, [(NOW - 5 * H, 0.9), (NOW, 0.7)])],          # too few points to draw
              "net_rx": [({"instance": "moudimash99"}, [(NOW - H, 1e5), (NOW, 1.5e5)])],
              "net_tx": [({"instance": "moudimash99"}, [(NOW - H, 2e5), (NOW, 2.4e5)])]},
        "at": NOW,
    }


def test_sites_traffic_and_movers():
    d = net.build(net_raw(), CAT)
    sites = {x["name"]: x for x in d["sites"]}
    assert d["sites"][0]["name"] == "Unmatched" and sites["Unmatched"]["state"] == "bad"
    assert sites["Unmatched"]["why"] == "down: answered 502" and sites["Unmatched"]["url"] == "https://unmatched.machaka.net"
    assert sites["machaka.net"]["state"] == "ok" and sites["machaka.net"]["url"] == "https://machaka.net"
    lab = next(x for x in d["sites"] if x["app"] == "lab")
    assert lab["state"] == "warn" and lab["p95"] == 5.0 and lab["url"] == "https://lab.machaka.net/machines/"
    assert d["classes"] == {"2xx": 12000, "3xx": 5600, "4xx": 1900, "5xx": 60}
    assert [m["name"] for m in d["machines"]] == ["Server", "Acer"]
    assert [a["name"] for a in d["movers"]] == ["bg-taste-match-fetch"]                    # beszel-agent left out
    assert [x["name"] for x in d["graphs"]["rpm"]] == ["Unmatched", "machaka.net"]
    assert [x["name"] for x in d["graphs"]["slow"]] == ["Unmatched"] and d["graphs"]["slow"][0]["points"][-1][1] == 20
    assert [x["name"] for x in d["graphs"]["traffic"]] == ["Server in", "Server out"]


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_network_page(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_status
    from app.sources import prom
    monkeypatch.setattr(net, "read", Fixed(net_raw()))
    monkeypatch.setattr(overview, "load_catalog", lambda: CAT)
    monkeypatch.setattr(prom, "alerts", Fixed([]))
    client = TestClient(site_status.app, root_path="/machines")
    for path in ("/network", "/network?range=7d"):
        r = client.get(path)
        assert r.status_code == 200 and "Traceback" not in r.text, path
    text = client.get("/network").text
    for words in ("The sites", "Requests a minute", "Slow answers", "Failed requests", "Traffic in and out",
                  "moved the most", "down: answered 502", 'href="https://unmatched.machaka.net"'):
        assert words in text, words
    assert 'aria-current="page"' in text and ">Network</span>" in text
