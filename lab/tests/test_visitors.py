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
    pts = {str(g): [5 if g % 2 else 2, 3.5, 1.0] for g in range(1, 10)}
    host = {"id": "susd:paul", "name": "paul", "n": 6, "rated": 8, "corr": 0.8, "match": 0.3, "low": 0.2, "high": 0.4,
            "evidence": 6.0, "counts": 0.375, "rres": {str(g): (0.6 if g % 2 else -0.4) for g in range(1, 10)}, "points": pts}
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
    assert "Finding your reviewers" in t and 'aria-live="polite"' in t and "/u/Some%20Body/status" in t
    visitors.run(visitors._q.get())
    t = c.get("/u/Some Body").text
    assert "Some Body's reviewers" in t and "Your closest match" in t and "/reviewers/susd:paul?u=Some%20Body" in t
    assert "Games your matches love" not in t                  # one recommendation section: Picked for you
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
    games = {1: {"name": "Brass", "year": 2018, "average": 8.0, "n": 50000, "weight": 3.9, "min_p": 2, "max_p": 4,
                 "time": 120, "links": {("category", "Economic"), ("designer", "Martin Wallace")}, "expansion": False},
             2: {"name": "Ark", "year": 2019, "average": 8.1, "n": 40000, "weight": 3.7, "min_p": 1, "max_p": 4,
                 "time": 150, "links": {("category", "Animals"), ("category", "Party Game")}, "expansion": False},
             7: {"name": "Concordia", "year": 2013, "average": 7.9, "n": 30000, "weight": 3.0, "min_p": 2, "max_p": 5,
                 "time": 100, "links": {("category", "Economic")}, "expansion": False},
             8: {"name": "Dixit", "year": 2008, "average": 7.2, "n": 60000, "weight": 1.2, "min_p": 3, "max_p": 6,
                 "time": 30, "links": {("category", "Party Game")}, "expansion": False},
             9: {"name": "Brass: Extra", "year": 2020, "average": 9.5, "n": 500, "weight": 3, "min_p": 2, "max_p": 4,
                 "time": 60, "links": {("category", "Economic")}, "expansion": True},
             **{g: {"name": f"G{g}", "year": 2000, "average": 7, "n": 5000, "weight": 2, "min_p": 2, "max_p": 4,
                    "time": 60, "expansion": False,
                    "links": {("category", "Economic")} if g % 2 else {("category", "Party Game")}} for g in (3, 4, 5, 6)}}
    return {"games": games, "typical": 7.0}

def test_insights_profile_picks_discover(lab, monkeypatch):
    from app.sources import insights
    monkeypatch.setattr(insights, "facts", Fixed(fake_facts()))
    s = taste.build(raw_result(), "x")
    prof = insights.taste_profile(s)
    assert [i["name"] for i in prof["category"]["up"]] == ["Economic"] and prof["category"]["up"][0]["n"] == 3
    assert [i["name"] for i in prof["category"]["down"]] == ["Party Game"]
    picks = insights.for_you(s)
    assert [e["gid"] for e in picks] == [7, 8] and picks[0]["why"][0]["name"] == "Economic"   # 9, an expansion: never
    assert picks[0]["likely"] > picks[1]["likely"] and all(1 <= e["likely"] <= 5 for e in picks)
    assert [e["gid"] for e in insights.for_you(s, players=6)] == [8]
    assert [e["gid"] for e in insights.for_you(s, weight="light")] == [8]
    assert insights.for_you(s, max_time=60)[0]["gid"] == 8
    d = insights.discover(s, min_hosts=1)
    assert d["games"] == 8 and d["gems"] and d["gems"][0]["above"] > 0 and d["overrated"][0]["above"] < 0


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
        assert o.get("/discover", follow_redirects=False).status_code == 200     # the same for everyone
        for path in ["/for-you?u=Some%20Body", "/discover"]:
            t = o.get(path).text
            assert taste.USER not in t and 'href="/games/' not in t, path
            assert "boardgamegeek.com/boardgame/" in t or "discover" in path, path   # one host here: no Discover rows
    finally:
        access.OPEN_HOSTS.discard("open.test")


def test_team_verdicts_and_unnamed_are_not_extra_opinions():
    from app.sources import insights
    paul = {"name": "Paul", "rres": {1: 1.0, 2: -1.0, 3: 0.5}, "points": {1: (5, 7, 1), 2: (2, 7, 1), 3: (4, 7, 0.4)},
            "sources": {3: [{"from": "team"}], 1: [{"from": "own"}, {"from": "team"}]}}
    unknown = {"name": "unknown 3", "rres": {1: 1.0, 2: 0.0, 3: 0.2}, "points": {1: (5, 7, 1), 2: (3, 7, 1), 3: (4, 7, 1)}}
    said = insights._reviewed({"reviewers": [paul, unknown]})
    assert set(said) == {1, 2} and len(said[1]) == 1          # game 3 was only the team's word; 'unknown' is nobody


def test_bgg_average_counts_less_when_few_rated_it():
    from app.sources import insights
    big = {"average": 9.0, "n": 100000}
    backers = {"average": 9.0, "n": 300}
    assert round(insights.bgg_average(big, 7.12), 2) == 9.0
    assert round(insights.bgg_average(backers, 7.12), 1) == 8.5         # as the For you page says
    assert insights.bgg_average({"average": 9.0, "n": 0}, 7.12) == 7.12


def test_likely_for_you_is_out_of_5_with_reasons(lab, monkeypatch):
    from app.sources import insights
    monkeypatch.setattr(insights, "facts", Fixed(fake_facts()))
    s = taste.build(raw_result(), "x")
    econ, party = insights.likely(s, 7), insights.likely(s, 8)
    assert 1 <= party["value"] < econ["value"] <= 5 and econ["why"][0]["name"] == "Economic"
    assert econ["bgg"] == insights.bgg_average(fake_facts()["games"][7], 7.0) / 2
    assert insights.likely(s, 12345) is None
    prof = insights.taste_profile(s)
    assert 0 < prof["category"]["up"][0]["lean"] <= 2.5                    # points out of 5


def test_game_page_cards_and_not_a_verdict(lab, monkeypatch):
    from starlette.testclient import TestClient
    from app import site_bgt
    from app.sources import bgt, insights
    import re as _re
    monkeypatch.setattr(insights, "facts", Fixed(fake_facts()))
    owner = taste.build(raw_result(), taste.USER)
    owner["at"] = time.time()
    monkeypatch.setattr(taste, "scores", Fixed(owner))
    monkeypatch.setattr(taste, "profile", Fixed({"games": {1: {"rating": 8}}}))
    monkeypatch.setattr(taste, "canonical", Fixed({}))
    monkeypatch.setattr(bgt, "channel_info", Fixed({"susd": {"name": "Shut Up & Sit Down", "url": ""}}))
    monkeypatch.setattr(bgt, "video_kinds", Fixed([("anticipated", _re.compile("most anticipated", _re.I), 0.0)]))
    base = {"id": 7, "name": "Concordia", "year": 2013, "rank": 30, "average": 7.9, "average_weight": 3.0,
            "min_players": 2, "max_players": 5, "playing_time": 100, "description": "", "designers": [],
            "mechanics": [], "categories": [], "min_time": None, "max_time": None, "min_age": None, "users_rated": 30000}
    verdicts = [{"video_id": "a", "rid": "susd:paul", "score": 5, "confidence": "high", "quote": "great",
                 "attribution": "own", "rated_at": "", "host": "Paul", "channel": "susd", "title": "Concordia review",
                 "duration": 600},
                {"video_id": "b", "rid": "susd:paul", "score": 1, "confidence": "high", "quote": "can't wait",
                 "attribution": "own", "rated_at": "", "host": "Paul", "channel": "susd",
                 "title": "Our most anticipated games", "duration": 600}]
    for v in verdicts:
        v["kind"], w = bgt.kind_of(v["title"])
        v["not_a_verdict"] = bgt.NOT_A_VERDICT.get(v["kind"]) if w == 0 else None
    monkeypatch.setattr(taste, "game", Fixed({**base, "verdicts": verdicts}))
    t = TestClient(site_bgt.app).get("/games/7").text
    assert "Traceback" not in t and "Likely for you" in t and "Your matches" not in t
    assert "hoping, before playing it" in t                    # the anticipated list is labelled...
    assert "average 5.0 out of 5" in t                          # ...and left out of the reviewers' average
    monkeypatch.setattr(taste, "game", Fixed({**base, "id": 1, "name": "Brass", "verdicts": verdicts}))
    t = TestClient(site_bgt.app).get("/games/1").text
    assert "Likely for you" not in t and "Reviewers" in t     # a game you rated: your score, not a guess



def _open_client():
    from starlette.applications import Starlette
    from starlette.routing import Host
    from starlette.testclient import TestClient
    from app import access, site_bgt
    return TestClient(access.AccessGate(Starlette(routes=[Host("open.test", app=site_bgt.app)])),
                      base_url="https://open.test")


def _owner_with_data(monkeypatch):
    """The owner's match with a marker no visitor result has: game 1 rated 2.2 out of 10 (shown "1.1")."""
    raw = raw_result()
    raw["user"]["1"] = [2.2, 7, 1]
    owner = taste.build(raw, taste.USER)
    owner["at"] = time.time()
    monkeypatch.setattr(taste, "scores", Fixed(owner))


def test_open_name_never_falls_back_to_the_owner(lab, monkeypatch):
    """The review's first finding: any ?u= that isn't a ready visitor must not show the owner's match there."""
    from app import access
    from app.sources import insights
    monkeypatch.setattr(insights, "facts", Fixed(fake_facts()))
    monkeypatch.setattr(taste, "host_verdicts", Fixed({"host": {"channel": "susd", "name": "paul"}, "verdicts": []}))
    monkeypatch.setattr(taste, "things", Fixed({}))
    monkeypatch.setattr(taste, "stats", Fixed({"verdicts": 1, "videos": 1, "week": 0, "channels": 1}))
    _owner_with_data(monkeypatch)
    monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {13: 6.5})
    monkeypatch.setattr(taste, "run_score", lambda ratings=None, timeout=0, **k: raw_result())
    visitors.ask("Some Body")
    visitors.run(visitors._q.get())
    access.OPEN_HOSTS.add("open.test")
    try:
        o = _open_client()
        for path in ["/reviewers/susd:paul?u=", "/reviewers/susd:paul?u=%20", "/reviewers/susd:paul?menu=1",
                     "/reviewers/susd:paul?u=Some%20Body&u=", "/for-you?u=", "/for-you?menu=1", "/for-you?u=%3Cx%3E"]:
            r = o.get(path, follow_redirects=False)
            assert r.status_code == 404, (path, r.status_code)
        r = o.get("/reviewers/susd:paul?u=nobody99", follow_redirects=False)     # a name without a match yet
        assert r.status_code == 303 and r.headers["location"] == "/u/nobody99"
        t = o.get("/reviewers/susd:paul?u=&u=Some%20Body").text                  # the last ?u= is the one read
        assert "Match with Some Body" in t and "You <b>4.0</b>" in t and "<b>1.1</b>" not in t
        monkeypatch.setattr(taste, "games", Fixed({1: {}}))
        assert o.get("/img/424242-s.jpg", follow_redirects=False).status_code == 404   # not a reviewed game
    finally:
        access.OPEN_HOSTS.discard("open.test")


def test_only_find_starts_a_match_and_old_ones_are_forgotten(lab, monkeypatch):
    from starlette.testclient import TestClient
    from app import site_bgt
    monkeypatch.setattr(taste, "stats", Fixed({"verdicts": 1, "videos": 1, "week": 0, "channels": 1}))
    monkeypatch.setattr(visitors, "fetch_ratings", lambda name: {13: 6.5})
    monkeypatch.setattr(taste, "run_score", lambda ratings=None, timeout=0, **k: raw_result())
    c = TestClient(site_bgt.app)
    t = c.get("/u/bot01").text                       # a crawler visiting /u/... starts nothing
    assert visitors._q.qsize() == 0 and "No match for bot01 yet" in t and "/find?name=bot01" in t
    assert c.get("/u/bot01/status").json() == {"state": None, "step": None, "ahead": None, "error": None}
    c.get("/find?name=old+timer")
    visitors.run(visitors._q.get())
    assert c.get("/u/old timer/status").json()["state"] == "done"
    with visitors._db() as conn:
        conn.execute("UPDATE visitors SET done = done - 2 * 86400, asked = asked - 2 * 86400")
    t = c.get("/u/old timer").text                    # a day later: no 404, a clear way to start again
    assert "No match for old timer yet" in t and visitors._q.qsize() == 0
    visitors.purge()
    with visitors._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM visitors").fetchone()[0] == 0


def test_queue_position_and_per_client_limit(lab, monkeypatch):
    first = visitors.ask("first one")
    second = visitors.ask("second one")
    assert first["ahead"] == 0 and second["ahead"] == 1                # not counting yourself
    monkeypatch.setattr(visitors, "_current", "someone else")
    assert visitors.status("first one")["ahead"] == 1                   # plus the match being worked on
    monkeypatch.setattr(visitors, "PER_CLIENT_HOUR", 2)
    visitors.ask("aaa1", client="1.2.3.4")
    visitors.ask("aaa2", client="1.2.3.4")
    with pytest.raises(visitors.VisitorError, match="a lot of matches"):
        visitors.ask("aaa3", client="1.2.3.4")
    assert visitors.ask("bbb1", client="5.6.7.8")["state"] == "queued"   # another client isn't held back


def test_six_plus_players_includes_bigger_games(lab, monkeypatch):
    from app.sources import insights
    f = fake_facts()
    f["games"][8].update(min_p=7, max_p=10)                               # Dixit, as a 7-10 player game here
    monkeypatch.setattr(insights, "facts", Fixed(f))
    s = taste.build(raw_result(), "x")
    assert 8 in [e["gid"] for e in insights.for_you(s, players=6)]
