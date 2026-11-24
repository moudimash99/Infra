"""The overview against saved Prometheus answers: failed and late jobs (with services.yaml's limits), projects from
the catalog, waiting pods, the problem list, and the page rendering with its filters' data.
Run: PYTHONPATH=. python -m pytest -q tests/test_status.py (from lab/)."""
import time

from app.sources import Reading, memory, overview
from tests.test_machines import G, raw_fixture, s

NOW = 1_800_000_000.0
CATALOG = {
    "groups": ["Board games", "Cats", "Job search", "Gmail", "Website and lab", "Monitoring", "Platform"],
    "services": {"bg-taste-match": {"group": "Board games"}, "cat-camera-jobs": {"group": "Cats"},
                 "free-motion": {"group": "Job search"}, "gmail-archiver": {"group": "Gmail"},
                 "job-scrapers": {"group": "Job search"}, "website": {"group": "Website and lab"}},
    "objects": {"apps/CronJob/bg-taste-match-fetch": {"service": "bg-taste-match", "max_age_h": 5},
                "apps/CronJob/bg-taste-match-gpu": {"service": "bg-taste-match", "max_age_h": 26},
                "apps/CronJob/cat-flash": {"service": "cat-camera-jobs", "max_age_h": 38},
                "apps/CronJob/email-remove": {"service": "gmail-archiver", "max_age_h": 2},
                "apps/Deployment/fm-controller": {"service": "free-motion"},
                "apps/StatefulSet/fm-browser-acer": {"service": "free-motion"},
                "pc_task:\\job-scrapers upwork round": {"service": "job-scrapers"}},
}


def raw():
    acer, srv = {"node": "acer-laptop"}, {"node": "moudimash99"}
    cj = lambda name, v: s(v / G, cronjob=name)              # s() multiplies by G
    job = lambda name, v, **kw: s(v / G, job_name=name, **kw)
    return {
        "up": [s(1 / G, instance="moudimash99"), s(1 / G, instance="acer-laptop")],
        "cpu": [s(13 / G, instance="moudimash99"), s(62 / G, instance="acer-laptop")],
        "cores": [s(8 / G, instance="moudimash99"), s(4 / G, instance="acer-laptop")],
        "disk": [s(67 / G, instance="moudimash99", mountpoint="/"), s(95 / G, instance="acer-laptop", mountpoint="/srv")],
        "temp": [s(64 / G, instance="moudimash99"), s(85 / G, instance="acer-laptop")],
        "pod_cpu": [s(0.9 / G, namespace="apps", pod="bg-taste-match-gpu-29924595-xc5mt", **acer)],
        "pod_info": [s(1 / G, namespace="apps", pod="bg-taste-match-gpu-29924595-xc5mt", uid="u1", **acer),
                     s(1 / G, namespace="apps", pod="fm-controller-88b97b7f-t4xdl", uid="u2", **srv),
                     s(1 / G, namespace="apps", pod="fm-webllm-7db466b596-x647r", uid="u3", node=""),
                     s(1 / G, namespace="tools", pod="lab-7fd4dbf58c-xwm9k", uid="u4", **acer)],
        # jobs: fetch failed last (good run 6 h ago, limit 5 h); gpu running; cat-flash fine; email-remove late
        "cj_suspend": [cj("bg-taste-match-fetch", 0), cj("bg-taste-match-gpu", 0), cj("cat-flash", 0),
                       cj("email-remove", 0), cj("cat-monitor", 1)],
        "cj_success": [cj("bg-taste-match-fetch", NOW - 6 * 3600), cj("bg-taste-match-gpu", NOW - 4 * 3600),
                       cj("cat-flash", NOW - 3 * 3600), cj("email-remove", NOW - 3 * 3600)],
        "cj_schedule": [cj("email-remove", NOW - 60), cj("cat-flash", NOW - 3 * 3600)],
        "cj_next": [cj("email-remove", NOW + 120), cj("cat-flash", NOW + 21 * 3600)],
        "job_owner": [job("bg-taste-match-fetch-1", 1, owner_name="bg-taste-match-fetch"),
                      job("bg-taste-match-gpu-1", 1, owner_name="bg-taste-match-gpu"),
                      job("cat-flash-1", 1, owner_name="cat-flash")],
        "job_start": [job("bg-taste-match-fetch-1", NOW - 2.5 * 3600), job("bg-taste-match-gpu-1", NOW - 3 * 3600),
                      job("cat-flash-1", NOW - 3.1 * 3600)],
        "job_end": [job("cat-flash-1", NOW - 3 * 3600)],
        "job_failed": [job("bg-taste-match-fetch-1", 1)],
        "job_active": [job("bg-taste-match-gpu-1", 1)],
        "job_node": [s(1 / G, created_by_name="bg-taste-match-fetch-1", **acer)],
        "oom": [s(1 / G, namespace="apps", pod="fm-controller-799c6b96bf-dzfd4", container="controller")],
        "pending": [s(600 / G, namespace="apps", pod="fm-webllm-7db466b596-x647r")],
        "pending_req": [s(1.0, namespace="apps", pod="fm-webllm-7db466b596-x647r")],
        "pc_age": [s(120 / G)],
        "pc_task_ok": [s(0, task="\\job-scrapers upwork round", status="failed", note="stopped: Upwork asked for a login"),
                       s(1 / G, task="\\GmailDailyReview", status="ok", note="")],
        "gpu_util": [s(0)], "gpu_mem": [s(1.0)], "gpu_mem_total": [s(4.0)], "gpu_temp": [s(80 / G)],
        "gpu_pods": [s(95 / G, pod="bg-taste-match-gpu-29924595-xc5mt")],
        "pushed": [{"at": NOW - 600, "pod": "bg-taste-match-shell-67b78c5458-m62kn", "by_uid": "u4", "node": "acer-laptop"},
                   {"at": NOW - 7200, "pod": "old-thing-abcde", "by_uid": None, "node": None}],
        "alerts": [], "sites": [{"name": "machaka.net", "url": "https://machaka.net", "up": False, "code": 502}],
        "memory": memory.build(raw_fixture()),
        "at": NOW,
    }


def test_projects_from_the_catalog():
    P = overview.Projects(CATALOG)
    assert P.group("apps", "bg-taste-match-gpu-2gib-1432") == "Board games"    # longest known name it starts with
    assert P.group("apps", "fm-webllm") == "Job search"                          # first word shared by all fm-*
    assert P.group("monitoring", "kube-state-metrics") == "Monitoring"
    assert P.group("apps", "nothing-known") == ""
    assert P.max_age_h("apps", "email-remove") == 2


def test_jobs_failed_late_running_fine():
    d = overview.build(raw(), CATALOG, now=NOW)
    jobs = {j["name"]: j for j in d["jobs"]}
    assert jobs["bg-taste-match-fetch"]["state"] == "bad" and jobs["bg-taste-match-fetch"]["m"] == "acer-laptop"
    assert jobs["email-remove"]["state"] == "warn" and "expected within 2 h" in jobs["email-remove"]["why"]
    assert jobs["bg-taste-match-gpu"]["state"] == "running" and jobs["bg-taste-match-gpu"]["p"] == "Board games"
    assert jobs["cat-flash"]["state"] == "ok" and jobs["cat-monitor"]["state"] == "paused"
    assert [j["name"] for j in d["jobs"]][:2] == ["bg-taste-match-fetch", "email-remove"]   # worst first


def test_problems_say_why():
    d = overview.build(raw(), CATALOG, now=NOW)
    texts = [p["text"] for p in d["problems"]]
    assert texts[0] in ("bg-taste-match-fetch failed", "machaka.net is down")            # the red ones first
    assert "email-remove is late" in texts
    assert "fm-controller was killed for using too much memory" in texts
    assert "fm-webllm has been waiting for room for 10 min" in texts
    assert "Acer disk filling up: 95% full" in texts
    assert "PC task job-scrapers upwork round failed" in texts
    assert not any("hot" in t for t in texts)                                             # 85 °C is under 90
    late = next(p for p in d["problems"] if p["text"] == "email-remove is late")
    assert late["p"] == "Gmail" and "expected within 2 h" in late["why"]


def test_machines_gpu_and_pushed_out():
    d = overview.build(raw(), CATALOG, now=NOW)
    acer = next(m for m in d["machines"] if m["node"] == "acer-laptop")
    assert acer["state"] == "bad" and acer["facts"]["disk"]["why"] == "fullest: /srv"
    assert acer["facts"]["cpu"]["why"] == "mostly bg-taste-match-gpu (0.9 cores)"
    assert d["gpu"]["util"] == 95 and d["gpu"]["users"] == [("bg-taste-match-gpu", 95.0)]
    assert [(e["name"], e["by"]) for e in d["pushed"]] == [("bg-taste-match-shell", "lab")]    # the old one is gone


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_page_renders(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_status
    monkeypatch.setattr(overview, "now", Fixed(overview.build(raw(), CATALOG, now=NOW)))
    r = TestClient(site_status.app, root_path="/machines").get("/")
    assert r.status_code == 200 and "Traceback" not in r.text
    for words in ("Anything wrong?", "How are the machines?", "What's using them?", "Did the jobs run?",
                  "Who's waiting for room?", "Is the GPU busy?", "Are the sites up?", "Did the PC's tasks run?"):
        assert words in r.text, words
    assert 'href="/machines/static/status.css' in r.text and 'data-f="only"' in r.text
    assert '<option value="Board games">' in r.text
    assert 'data-m="acer-laptop" data-p="Board games" data-bad="1"' in r.text                # the failed fetch


def test_memory_tab_and_published_route(monkeypatch):
    """lab.machaka.net/machines is the overview, /machines/memory the memory page; both need the login."""
    from starlette.testclient import TestClient
    from app import main, site_status
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw_fixture())))
    monkeypatch.setattr(memory, "low", Fixed(None))
    r = TestClient(site_status.app, root_path="/machines").get("/memory")
    assert r.status_code == 200 and "Where the memory goes" in r.text and 'href="/machines/memory"' in r.text
    assert any(sub is site_status.app for _, _, _, sub, _ in main.PATH_SITES)
    gate = TestClient(main.app, base_url="https://lab.machaka.net")
    assert gate.get("/machines/").status_code == 403 and gate.get("/machines/memory").status_code == 403


def test_keep_fresh_fills_the_cache(monkeypatch):
    monkeypatch.setattr(overview, "fetch", raw)
    monkeypatch.setattr(overview, "load_catalog", lambda: CATALOG)
    overview.now._cache.clear()
    overview.keep_fresh()
    got = overview.now.get()
    assert got.ok and got.data["jobs"][0]["name"] == "bg-taste-match-fetch"
