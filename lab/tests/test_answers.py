"""The answers page (/) against saved Prometheus answers: the memory pile adds up to what the machine uses, the
lowest point names who had grown, kills say by whom, the timeline uses the jobs' real runs and folds steady apps
into one line, and the page renders with its interactive charts' data.
Run: PYTHONPATH=. python -m pytest -q tests/test_answers.py (from lab/)."""
import time

from app.sources import Reading, answers, apps, jobruns, memory, overview
from tests.test_apps import CAT, apps_raw
from tests.test_jobs import week_raw
from tests.test_machines import G, raw_fixture
from tests.test_status import CATALOG, NOW, raw as overview_raw

STEP = 300
START = int(NOW // STEP * STEP) - 6 * STEP
END = START + 6 * STEP


def pts(*vals):
    return [(START + i * STEP, v) for i, v in enumerate(vals)]


def ans_raw():
    srv, acer = "moudimash99", "acer-laptop"
    gb = lambda *v: pts(*[x * G for x in v])
    return {
        "start": START, "end": END, "step": STEP,
        f"total:{srv}": [({}, gb(7.5, 7.5, 7.5, 7.5, 7.5, 7.5, 7.5))],
        f"used:{srv}": [({}, gb(4, 4, 6.5, 7.1, 5, 4, 4))],
        f"room:{srv}": [({}, gb(7.4, 7.4, 7.4, 7.4, 7.4, 7.4, 7.4))],
        f"apps:{srv}": [({"namespace": "apps", "pod": "fm-controller-88b97b7f-t4xdl"}, gb(1.2, 1.2, 2.5, 3.0, 1.5, 1.2, 1.2)),
                        ({"namespace": "monitoring", "pod": "grafana-79dc6b9df6-jwjtp"}, gb(0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3)),
                        ({"namespace": "apps", "pod": "tiny-5d9f7c6b8d-zzzzz"}, gb(0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01))],
        f"k3s:{srv}": [({}, gb(1, 1, 1, 1, 1, 1, 1))],
        "docker": [({"name": "quizzical_goodall"}, gb(0, 0, 1.5, 1.6, 0.5, 0, 0))],
        f"total:{acer}": [({}, gb(7.6, 7.6, 7.6, 7.6, 7.6, 7.6, 7.6))],
        f"used:{acer}": [({}, gb(4, 4, 4, 4, 4, 4, 4))],
        f"apps:{acer}": [({"namespace": "apps", "pod": "frigate-64b885b9c7-rf2bv"}, gb(1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2))],
        "running": [({"namespace": "apps", "pod": "frigate-64b885b9c7-rf2bv"}, pts(1, 1, 1, 1, 1, 1, 1)),
                    ({"namespace": "apps", "pod": "fm-controller-88b97b7f-t4xdl"}, pts(1, 1, 1, 0, 0, 1, 1))],
        "waiting": [({"namespace": "apps", "pod": "fm-webllm-7db466b596-x647r"}, pts(0, 1, 1, 0, 0, 0, 0))],
        "where": [({"namespace": "apps", "pod": "frigate-64b885b9c7-rf2bv", "node": acer}, pts(1)),
                  ({"namespace": "apps", "pod": "fm-controller-88b97b7f-t4xdl", "node": srv}, pts(1))],
        "pushed": [({"namespace": "apps", "pod": "bg-taste-match-fetch-29923685-bphpf", "reason": "PreemptionByScheduler"},
                    [(START + 2 * STEP, 1.0)])],
        "kernel": [({"instance": srv}, [(START + 3 * STEP, 1.0)])],
        f"temp:{srv}": [({}, pts(60, 61, 62, 63, 62, 61, 60))],
        "gpu_pods": [({"pod": "bg-taste-match-gpu-29924595-xc5mt"}, pts(0, 90, 95, 0, 0, 0, 0))],
    }


def test_memory_pile_and_answer():
    d = answers.build(ans_raw(), None, [], "/machines")
    srv = d["mem"]["moudimash99"]
    names = [s["name"] for s in srv["chart"]["series"]]
    assert names[0] == "fm-controller" and "quizzical_goodall (Docker)" in names
    assert names[-1].startswith("outside them") and "smaller apps (1)" in names
    top = [sum(s["values"][3] or 0 for s in srv["chart"]["series"])]
    assert abs(top[0] - 7.1) < 0.01                                  # the pile reaches what the machine uses
    x = srv["answer"]
    assert round(x["low"], 2) == 0.4 and x["grew"][0][1] == "fm-controller"
    assert srv["chart"]["series"][0]["href"] == "/machines/apps/apps/fm-controller"


def test_kills_timeline_heat():
    week = jobruns.build(week_raw(), now=NOW)
    rows = apps.build(apps_raw(), memory.build(raw_fixture()), CAT, now=NOW)["rows"]
    d = answers.build(ans_raw(), week, rows, "")
    whats = [(k["name"], k["what"]) for k in d["kills"]]
    assert ("Server", "ran out of memory") in whats and ("bg-taste-match-fetch", "pushed out") in whats
    acer = {r["name"]: r for r in d["tables"]["acer-laptop"]["rows"]}
    assert acer["frigate"]["rank"] == "homelab-critical" and acer["frigate"]["booked"] == 1.5 * G
    assert acer["frigate"]["segs"] and not acer["frigate"]["trouble"]
    srv = {r["name"]: r for r in d["tables"]["moudimash99"]["rows"]}
    assert len(srv["fm-controller"]["segs"]) == 2                    # a break in the middle
    assert any(r["name"] == "fm-webllm" for r in d["tables"][""]["rows"])   # waited, never placed
    runs = acer["email-remove"]["segs"]                             # a job's bars are its runs (one a minute here)
    assert len(runs) >= 8 and {x[2] for x in runs} <= {"running", "failed"}       # the range is the last 30 min
    assert [s["name"] for s in d["heat"]["series"]] == ["Server"] and d["gpu"]["series"][0]["name"] == "bg-taste-match-gpu"


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_answers_page(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_status
    from app.sources import prom
    monkeypatch.setattr(answers, "read", Fixed(ans_raw()))
    monkeypatch.setattr(overview, "now", Fixed(overview.build(overview_raw(), CATALOG, now=NOW)))
    monkeypatch.setattr(jobruns, "week", Fixed(jobruns.build(week_raw(), now=NOW)))
    monkeypatch.setattr(apps, "read", Fixed(apps_raw()))
    monkeypatch.setattr(memory, "now", Fixed(memory.build(raw_fixture())))
    monkeypatch.setattr(overview, "load_catalog", lambda: CAT)
    monkeypatch.setattr(prom, "alerts", Fixed([]))
    client = TestClient(site_status.app, root_path="/machines")
    for path in ("/", "/?range=7d", "/summary"):
        r = client.get(path)
        assert r.status_code == 200 and "Traceback" not in r.text, path
    text = client.get("/").text
    for words in ("Why is memory high, and who is using it?", "Was anything killed or pushed out?",
                  "Did each job run?", "Does every app get in, or wait for room? What rank does it have",
                  "Show system apps", "data-system", "Is anything too hot?", "tchart.js", 'data-src="c-mem-moudimash99"', 'data-rows="rows-acer-laptop"'):
        assert words in text, words


def test_schedules_and_system_apps():
    from app.sources import cron
    assert cron.words("*/3 * * * *", "Europe/Paris") == "every 3 min"
    assert cron.words("20 0,19 * * *", "Europe/Paris") == "daily at 00:20 and 19:20"
    assert cron.words("5 */2 * * *", "local") == "every 2 hours at :05"
    assert cron.words("0 0 1 1 *", "Europe/Paris").startswith("once a year")
    assert len(cron.times("*/3 * * * *", "Europe/Paris", NOW - 3600, NOW)) in (20, 21)
    raw = {**ans_raw(), "cj_info": [({"cronjob": "email-remove", "schedule": "*/3 * * * *", "timezone": "Europe/Paris"}, 1.0),
                                     ({"cronjob": "boardgamefinder", "schedule": "30 4 * * *", "timezone": "Europe/Paris"}, 1.0)],
           "cj_suspend": [({"cronjob": "boardgamefinder"}, 1.0)]}
    week = jobruns.build(week_raw(), now=NOW)
    d = answers.build(raw, week, [], "", CAT)
    em = d["sched"]["email-remove"]
    assert em["words"] == "every 3 min" and em["due"] in (10, 11) and em["ran"] <= em["due"]
    assert d["sched"]["boardgamefinder"]["paused"] and d["sched"]["boardgamefinder"]["due"] == 0
    assert answers.is_system(overview.Projects(CAT), "monitoring", "node-exporter")
    assert not answers.is_system(overview.Projects(CAT), "apps", "frigate")
