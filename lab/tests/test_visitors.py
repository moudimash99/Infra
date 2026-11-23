"""Anyone's matches: BGG's collection XML, the queue, and the pages.
Run from Infra/lab: PYTHONPATH=. python -m pytest -q tests/test_visitors.py"""
import time
import xml.etree.ElementTree as ET

import pytest

from app import store
from app.sources import Reading, taste, visitors

XML = """<items totalitems="3"><item objectid="13" subtype="boardgame"><name>Catan</name>
<stats><rating value="6.5"><average value="7.1"/></rating></stats></item>
<item objectid="822" subtype="boardgame"><stats><rating value="N/A"><average value="7.4"/></rating></stats></item>
<item objectid="31260" subtype="boardgame"><stats><rating value="9"><average value="8.0"/></rating></stats></item></items>"""


class Fixed:
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


def raw_result():
    pts = {str(g): [5 if g % 2 else 2, 3.5, 1.0] for g in range(1, 9)}
    host = {"id": "susd:paul", "name": "paul", "n": 6, "rated": 8, "corr": 0.8, "match": 0.3, "low": 0.2, "high": 0.4,
            "evidence": 6.0, "counts": 0.375, "rres": {str(g): (0.6 if g % 2 else -0.4) for g in range(1, 9)}, "points": pts}
    return {"user": {str(g): [8 if g % 2 else 4, 7, 1] for g in range(1, 7)},
            "ures": {str(g): (1.0 if g % 2 else -1.0) for g in range(1, 7)}, "reviewers": [host]}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LAB_DB", str(tmp_path / "lab.db"))
    store.init()
    monkeypatch.setattr(visitors, "_q", __import__("queue").Queue())
    monkeypatch.setattr(visitors, "_recent", [])
    monkeypatch.setattr(visitors, "_parsed", {})
    monkeypatch.setattr(visitors, "_start", lambda: None)           # the tests run the queue by hand
    monkeypatch.setattr(taste, "channel_info", Fixed({"susd": {"name": "Shut Up & Sit Down", "url": ""}}))
    return tmp_path


def test_parse_ratings():
    assert visitors.parse_ratings(ET.fromstring(XML)) == {13: 6.5, 31260: 9.0}
    with pytest.raises(visitors.VisitorError, match="no BoardGameGeek user"):
        visitors.parse_ratings(ET.fromstring("<errors><error><message>Invalid username specified</message></error></errors>"))


def test_names():
    assert visitors.valid("moudimash99") and visitors.valid("Tom  Vasel") and visitors.key_of(" Tom  Vasel ") == "tom vasel"
    assert not visitors.valid("") and not visitors.valid("x") and not visitors.valid("<script>") and not visitors.valid("a" * 41)


def test_queue_runs_a_match(lab, monkeypatch):
    monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {13: 6.5, 31260: 9.0})
    seen = {}
    monkeypatch.setattr(taste, "run_score", lambda ratings=None, timeout=0, **k: seen.setdefault("r", ratings) and raw_result())
    st = visitors.ask("Some Body")
    assert st["state"] == "queued" and visitors.ask("some body")["state"] == "queued"   # same name, one job
    visitors.run(visitors._q.get())
    assert seen["r"] == {13: 6.5, 31260: 9.0}
    st = visitors.status("SOME BODY")
    assert st["state"] == "done" and st["rated"] == 2
    res = visitors.result("some body")
    assert res["username"] == "Some Body" and res["ranked"][0]["id"] == "susd:paul" and res["ranked"][0]["rank"] == 1
    assert visitors.ask("Some Body")["state"] == "done"                                   # reused for a day


def test_queue_errors_in_plain_words(lab, monkeypatch):
    def nobody(name):
        raise visitors.VisitorError("There is no BoardGameGeek user with that name.")
    monkeypatch.setattr(visitors, "fetch_ratings", nobody)
    visitors.ask("ghost99")
    visitors.run(visitors._q.get())
    assert visitors.status("ghost99")["error"] == "There is no BoardGameGeek user with that name."
    monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {})
    visitors.ask("empty99")
    visitors.run(visitors._q.get())
    assert "hasn't rated" in visitors.status("empty99")["error"]
    monkeypatch.setattr(visitors, "PER_HOUR", 2)
    with pytest.raises(visitors.VisitorError, match="Lots of people"):
        visitors.ask("third99")


def test_pages(lab, monkeypatch):
    from starlette.testclient import TestClient
    from app import site_bgt
    monkeypatch.setattr(taste, "stats", Fixed({"verdicts": 9000, "videos": 1200, "week": 5, "channels": 25}))
    monkeypatch.setattr(taste, "things", Fixed({1: {"name": "Brass", "average": 8, "year": 2018}}))
    monkeypatch.setattr(taste, "host_verdicts", Fixed({"host": {"channel": "susd", "name": "paul"}, "verdicts": [
        {"score": 5, "game": "Brass", "game_name": "Brass", "gid": 1, "video_id": "v1", "title": "Brass", "quote": ""}]}))
    monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {13: 6.5})
    monkeypatch.setattr(taste, "run_score", lambda ratings=None, timeout=0, **k: raw_result())
    c = TestClient(site_bgt.app)
    t = c.get("/find").text
    assert "Which board game reviewers share your taste?" in t and "9,000 verdicts" in t
    assert "doesn" in c.get("/find?name=%3Cx%3E").text                      # not a username: said on the page
    r = c.get("/find?name=Some+Body", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/u/Some%20Body"
    t = c.get("/u/Some Body").text
    assert "Finding your reviewers" in t and 'http-equiv="refresh"' in t
    visitors.run(visitors._q.get())
    t = c.get("/u/Some Body").text
    assert "Some Body's reviewers" in t and "Your closest match" in t and "/reviewers/susd:paul?u=Some%20Body" in t
    assert "Games your matches love" in t
    t = c.get("/reviewers/susd:paul?u=Some%20Body").text
    assert "Match with Some Body&#39;s taste" in t and "Some Body's reviewers" in t and "Traceback" not in t
    assert c.get("/u/%3Cx%3E").status_code == 404
    t = c.get("/about").text
    assert "How it works" in t and "38%" in t and "language model" in t


def test_visitors_are_forgotten_after_a_day(lab, monkeypatch):
    monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {13: 6.5})
    monkeypatch.setattr(taste, "run_score", lambda ratings=None, timeout=0, **k: raw_result())
    visitors.ask("old timer")
    visitors.run(visitors._q.get())
    assert visitors.result("old timer")
    with visitors._db() as c:
        c.execute("UPDATE visitors SET done = done - 2 * 86400, asked = asked - 2 * 86400")
    assert visitors.result("old timer") is None                     # stale results are not shown
    visitors.purge()
    with visitors._db() as c:
        assert c.execute("SELECT COUNT(*) FROM visitors").fetchone()[0] == 0   # ratings and result deleted


def test_open_name_serves_only_the_visitor_pages(lab, monkeypatch):
    """On a name without a login, every owner page is 404 and the public ones carry none of the owner's data."""
    from starlette.applications import Starlette
    from starlette.routing import Host
    from starlette.testclient import TestClient
    from app import access, site_bgt
    access.OPEN_HOSTS.add("open.test")
    try:
        monkeypatch.setattr(taste, "stats", Fixed({"verdicts": 9000, "videos": 1200, "week": 5, "channels": 25}))
        monkeypatch.setattr(taste, "things", Fixed({1: {"name": "Brass", "average": 8, "year": 2018}}))
        monkeypatch.setattr(taste, "host_verdicts", Fixed({"host": {"channel": "susd", "name": "paul"}, "verdicts": [
            {"score": 5, "game": "Brass", "game_name": "Brass", "gid": 1, "video_id": "abcdefghijk", "title": "B", "quote": ""}]}))
        monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {13: 6.5})
        monkeypatch.setattr(taste, "run_score", lambda ratings=None, timeout=0, **k: raw_result())
        visitors.ask("Some Body")
        visitors.run(visitors._q.get())
        gate = access.AccessGate(Starlette(routes=[Host("open.test", app=site_bgt.app)]))
        c = TestClient(gate, base_url="https://open.test")
        r = c.get("/", follow_redirects=False)
        assert r.status_code in (302, 303, 307) and r.headers["location"] == "/find"
        for path in ["/reviewers", "/reviewers/susd:paul", "/games", "/games/1", "/videos", "/v/abcdefghijk",
                     "/pipeline", "/results", "/census", "/search?q=brass", "/img/../pipeline", "/static/../site_bgt.py"]:
            assert c.get(path, follow_redirects=False).status_code == 404, path
        for path in ["/find", "/about", "/u/Some Body", "/reviewers/susd:paul?u=Some%20Body"]:
            r = c.get(path)
            assert r.status_code == 200, path
            t = r.text
            assert taste.USER not in t, path                                  # the owner's name appears nowhere
            for private in ('href="/pipeline"', 'href="/games/', 'href="/v/', 'href="/videos"', "Machaka lab", "lab.machaka.net"):
                assert private not in t, (path, private)
        t = c.get("/reviewers/susd:paul?u=Some%20Body").text
        assert "https://boardgamegeek.com/boardgame/" in t and "https://www.youtube.com/watch?v=abcdefghijk" in t
        r = c.get("/reviewers/susd:paul?u=nobody99", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/u/nobody99"     # never the owner's match instead
    finally:
        access.OPEN_HOSTS.discard("open.test")
