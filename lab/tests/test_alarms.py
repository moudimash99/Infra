"""The Alarms page against saved answers: each rule's plain title, its state (firing, about to fire, quiet), its 7
days from Prometheus's ALERTS series (two close points are one firing), what is muted, and the page rendering.
Run: PYTHONPATH=. python -m pytest -q tests/test_alarms.py (from lab/)."""
import time

from app.sources import Reading, alarms
from tests.test_status import NOW

H = 3600


def rule(name, summary, sev="warning", state="inactive", alerts=(), duration=300, description="", kind=None):
    labels = {"severity": sev, **({"kind": kind} if kind else {})}
    return {"type": "alerting", "name": name, "state": state, "duration": duration, "labels": labels,
            "annotations": {"summary": summary, "description": description}, "alerts": list(alerts), "health": "ok"}


def alarms_raw():
    return {
        "rules": [
            {"name": "nodes", "rules": [
                rule("NodeHot", "{{ $labels.instance }}: running hot, {{ $value | humanize }} °C",
                     description="The hottest sensor has been above 90 °C for 5 minutes. Laptops throttle."),
                rule("NodeDown", "{{ $labels.instance }} doesn't answer", "critical", "firing",
                     [{"state": "firing", "labels": {"instance": "acer-laptop"}}], 120)]},
            {"name": "scheduled-jobs", "rules": [
                rule("JobFailed", "{{ $labels.job_name }}: a run failed", state="pending",
                     alerts=[{"state": "pending"}], duration=0, kind="event"),
                rule("BackupsMissed", "{{ $labels.cronjob }}: no good nightly backup in over a day")]},
        ],
        "history": [({"alertname": "NodeHot", "severity": "warning"},
                     [(NOW - 30 * H + k * 300, 1.0) for k in range(4)] + [(NOW - 2 * H, 1.0)]),   # two firings
                    ({"alertname": "NodeDown", "severity": "critical"}, [(NOW - 600, 1.0), (NOW - 300, 1.0)])],
        "silences": [{"status": {"state": "active"}, "matchers": [{"name": "alertname", "value": "GPUHot", "isEqual": True}],
                      "comment": "summer", "createdBy": "me", "endsAt": "2026-12-01T00:00:00Z"},
                     {"status": {"state": "expired"}, "matchers": [], "comment": "old"}],
        "at": NOW,
    }


def test_rules_titles_states_and_week():
    d = alarms.build(alarms_raw(), now=NOW)
    rows = {r["name"]: r for r in d["rules"]}
    assert d["rules"][0]["name"] == "NodeDown" and rows["NodeDown"]["state"] == "bad"
    assert rows["NodeDown"]["title"] == "A machine doesn't answer" and rows["NodeDown"]["text"].startswith("firing now")
    assert rows["JobFailed"]["state"] == "info" and rows["JobFailed"]["title"] == "A job: a run failed"
    assert rows["NodeHot"]["title"] == "A machine is running hot (over 90 °C)"
    assert rows["NodeHot"]["what"] == "The hottest sensor has been above 90 °C for 5 minutes."
    assert rows["NodeHot"]["week"] == 2 and rows["NodeHot"]["area"] == "Machines"
    assert sum(1 for c in rows["NodeHot"]["cells"] if c["state"]) in (1, 2)          # one or two days, by the clock
    assert rows["BackupsMissed"]["text"] == "quiet all week" and rows["BackupsMissed"]["area"] == "Jobs"
    assert [s["what"] for s in d["silences"]] == ["alertname=GPUHot"]


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def test_alarms_page(monkeypatch):
    from starlette.testclient import TestClient
    from app import site_status
    from app.sources import prom
    monkeypatch.setattr(alarms, "read", Fixed(alarms_raw()))
    monkeypatch.setattr(prom, "alerts", Fixed([{"name": "NodeDown", "severity": "critical",
                                                 "summary": "acer-laptop doesn't answer", "since": None,
                                                 "labels": {"instance": "acer-laptop"}}]))
    text = TestClient(site_status.app, root_path="/machines").get("/alarms").text
    for words in ("Firing now", "Every alarm", "Muted", "acer-laptop doesn", "A machine is running hot", "summer"):
        assert words in text, words
    assert 'href="/machines/acer"' in text and 'href="/machines/alarms"' in text            # the bell links here
    assert text.count('class="day ') >= 7 * 4
