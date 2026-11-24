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


def fake_facts():
    return {1: {"name": "Brass", "year": 2018, "average": 8.0, "weight": 3.9, "min_p": 2, "max_p": 4, "time": 120,
                "links": {("category", "Economic"), ("designer", "Martin Wallace")}},
            2: {"name": "Ark", "year": 2019, "average": 8.1, "weight": 3.7, "min_p": 1, "max_p": 4, "time": 150,
                "links": {("category", "Animals"), ("category", "Party Game")}},
            7: {"name": "Concordia", "year": 2013, "average": 7.9, "weight": 3.0, "min_p": 2, "max_p": 5, "time": 100,
                "links": {("category", "Economic")}},
            8: {"name": "Dixit", "year": 2008, "average": 7.2, "weight": 1.2, "min_p": 3, "max_p": 6, "time": 30,
                "links": {("category", "Party Game")}},
            **{g: {"name": f"G{g}", "year": 2000, "average": 7, "weight": 2, "min_p": 2, "max_p": 4, "time": 60,
                   "links": {("category", "Economic")} if g % 2 else {("category", "Party Game")}} for g in (3, 4, 5, 6)}}


def test_insights_profile_picks_discover(lab, monkeypatch):
    from app.sources import insights
    monkeypatch.setattr(insights, "facts", Fixed(fake_facts()))
    s = taste.build(raw_result(), "x")
    prof = insights.taste_profile(s)
    assert [i["name"] for i in prof["category"]["up"]] == ["Economic"] and prof["category"]["up"][0]["n"] == 3
    assert [i["name"] for i in prof["category"]["down"]] == ["Party Game"]
    picks = insights.for_you(s)
    assert [e["gid"] for e in picks] == [7, 8] and picks[0]["why"][0]["name"] == "Economic"
    assert picks[0]["expected"] > picks[1]["expected"]
    assert [e["gid"] for e in insights.for_you(s, players=6)] == [8]
    assert [e["gid"] for e in insights.for_you(s, weight="light")] == [8]
    assert insights.for_you(s, max_time=60)[0]["gid"] == 8
    d = insights.discover(s, min_hosts=1)
    assert d["games"] == 8 and d["gems"] and d["gems"][0]["z"] > 0 and d["overrated"][0]["z"] < 0


def test_new_pages_owner_and_visitor_and_open(lab, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Host
    from starlette.testclient import TestClient
    from app import access, site_bgt
    from app.sources import insights
    monkeypatch.setattr(insights, "facts", Fixed(fake_facts()))
    monkeypatch.setattr(taste, "stats", Fixed({"verdicts": 9000, "videos": 1200, "week": 5, "channels": 25}))
    monkeypatch.setattr(taste, "things", Fixed({1: {"name": "Brass", "average": 8, "year": 2018}}))
    monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {13: 6.5})
    monkeypatch.setattr(taste, "run_score", lambda ratings=None, timeout=0, **k: raw_result())
    owner = taste.build(raw_result(), taste.USER)
    owner["at"] = time.time()
    monkeypatch.setattr(taste, "scores", Fixed(owner))
    visitors.ask("Some Body")
    visitors.run(visitors._q.get())
    c = TestClient(site_bgt.app)
    for path in ["/for-you", "/for-you?players=6", "/for-you?weight=light&time=60", "/for-you?weight=bogus&players=99",
                 "/discover", "/for-you?u=Some%20Body", "/discover?u=Some%20Body"]:
        r = c.get(path)
        assert r.status_code == 200 and "Traceback" not in r.text, path
    assert "Concordia" in c.get("/for-you").text and "Dixit" in c.get("/for-you?players=6").text
    assert "Concordia" not in c.get("/for-you?players=6").text and "u=Some%20Body" in c.get("/for-you?u=Some%20Body").text
    assert "Hidden gems" in c.get("/discover").text
    t = c.get("/u/Some Body").text
    assert "Picked for you" in t and "Your taste profile" in t and "Economic" in t
    assert c.get("/for-you?u=nobody99", follow_redirects=False).headers["location"] == "/u/nobody99"
    access.OPEN_HOSTS.add("open.test")
    try:
        o = TestClient(access.AccessGate(Starlette(routes=[Host("open.test", app=site_bgt.app)])), base_url="https://open.test")
        assert o.get("/for-you", follow_redirects=False).status_code == 404      # the owner's picks: never public
        assert o.get("/discover", follow_redirects=False).status_code == 404
        for path in ["/for-you?u=Some%20Body", "/discover?u=Some%20Body"]:
            t = o.get(path).text
            assert taste.USER not in t and 'href="/games/' not in t, path
            assert "boardgamegeek.com/boardgame/" in t or "discover" in path, path   # one host here: no Discover rows
    finally:
        access.OPEN_HOSTS.discard("open.test")
