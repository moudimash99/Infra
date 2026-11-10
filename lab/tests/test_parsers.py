"""The log parsers and charts against real log shapes. Run: python -m pytest lab/tests (from the Infra root, with
lab/ on PYTHONPATH)."""
from app import charts, logs

FETCH = """=== 2026-11-10T01:05:00Z download hc
199 videos
[1/199] ok    abc123def45  Some review  (20 MB)
[2/199] FAIL  zzz123def45  Other  (HTTP Error 403: Forbidden)
=== 2026-11-10T02:00:00Z download cotb
41 videos
[7/41] ok    GSAZagxslvU  Castle Combo Review  (3 MB)
"""

BLOCKED = """=== 2026-11-10T01:05:00Z download hc
[1/9] FAIL  a  t  (403)
5 failures in a row: stopping so YouTube doesn't flag us; rerun later.
=== 2026-11-10T01:09:00Z YouTube is refusing downloads: pass stopped at hc
"""

WHISPER = """=== 2026-11-10T01:17:27Z whisper rahdo
87 videos to transcribe
model medium.en on cuda (int8)
[18/87] uLiJ6tUcGWc  14 min audio in 2.7 min
[19/87] FAILED DD0IGKS0Y5k  RuntimeError: boom
"""

BGF = """==============================================
2026-11-10 07:27:42 Running step: Scraping prices (src/boardgamefinder/price_finder.py)
==============================================
==============================================
2026-11-10 07:50:24 Running step: Classifying new or updated listings with the LLM (scripts/classify_postings_with_llm.py)
==============================================
Step classify_postings_with_llm FAILED (Exit Code 1)
STDOUT: Found 599 unclassified / updated listing(s) to evaluate.
  Batch failed: fm: fm-llm unreachable at http://192.168.1.116:8792
"""


def test_fetch_progress():
    s = logs.bgt(FETCH)
    assert [x["what"] for x in s["stages"]] == ["download hc", "download cotb"]
    assert s["now"]["pos"] == (7, 41) and s["ok"] == 2 and s["fail"] == 1 and not s["blocked"]


def test_youtube_block():
    s = logs.bgt(BLOCKED)
    assert s["blocked"] and any("stopping so YouTube" in f for f in s["fails"])


def test_whisper_speed():
    st = logs.bgt(WHISPER)["now"]
    assert st["audio_min"] == 14 and abs(st["took_min"] - 2.7) < 1e-9 and st["fail"] == 1


def test_bgf_steps():
    s = logs.bgf(BGF)
    assert [x["key"] for x in s["steps"]] == ["price_finder", "classify_postings_with_llm"]
    failed = s["steps"][1]
    assert failed["failed"] and failed["code"] == 1 and "unreachable" in failed["errors"][0]


def test_ticks_whole_for_counts():
    assert charts.nice_ticks(9, whole=True) == [0, 5, 10]
    assert charts.nice_ticks(3, whole=True) == [0, 1, 2, 3]


def test_charts_render():
    assert "<svg" in charts.columns([{"label": "a", "value": 3}, {"label": "", "value": 0}])
    out = charts.lines([{"name": "A", "slot": 1, "points": [(0, 1), (10, 2)]},
                        {"name": "B", "slot": 2, "points": [(0, 2), (10, 5)]}])
    assert "legend" in out and "data-lines" in out


def test_access_fails_closed():
    from app import access
    access.DEV = False
    for host, path in [("catcam.machaka.net", "/"), ("CATCAM.machaka.net.", "/media/latest.jpg"),
                       ("catcam.machaka.net:443", "/"), ("10.42.1.50:8080", "/"), ("lab.tools.svc", "/"),
                       ("evil.example", "/api/summary/catcam"), ("", "/catcam/"), ("lab.machaka.net", "/catcam/"),
                       ("lab.tools.svc", "/catcam/media/latest.jpg")]:
        assert access.needs_token(host, path), (host, path)
    for host, path in [("10.42.1.50:8080", "/healthz"), ("lab.tools.svc", "/api/summary/catcam"),
                       ("lab.tools.svc.cluster.local", "/api/summary/deals")]:
        assert not access.needs_token(host, path), (host, path)
    assert not access.check("") and not access.check("not-a-jwt")


def test_nav_links():
    """A site's sections are its tabs; the other sites are in the menu, at their own names (or, on a development
    machine, at the paths next to it)."""
    from app import web
    base = web.templates.get_template("base.html")
    site = {**web.SITES["deals"], "nav": [("/", "Deals", "tag"), ("/new", "New", "zap")]}
    own = base.render(R="", site=site, active="/new", me="deals", SITES=list(web.SITES.values()))
    assert 'href="/new" class="on"' in own and 'href="https://tastematch.machaka.net"' in own
    dev = base.render(R="/deals", site=site, active="/", me="deals",
                      SITES=[{**s, "url": f"/{s['key']}/"} for s in web.SITES.values()])
    assert 'href="/deals/new"' in dev and 'href="/tastematch/"' in dev and 'data-site="deals"' in dev


def test_tile_rows():
    from app import summary
    fetch = {"kind": "bg-taste-match-fetch",
             "log": {"now": {"what": "download dt", "label": "Downloading The Dice Tower", "pos": (30, 537)}}}
    assert summary._running(fetch) == ("download", "now The Dice Tower 30/537")
    spk = {"kind": "bg-taste-match-gpu", "log": {"now": {"what": "speaker separation", "label": "Finding speakers",
                                                          "pos": (5, 40)}}}
    assert summary._running(spk) == ("speakers", "running 5/40")
    assert summary._running({"kind": "bg-taste-match-gpu", "log": {"now": None}}) == ("transcribe", "starting")


def test_bridge_values():
    import time
    from app import access
    code = access.sign({"k": "code", "h": "deals.machaka.net", "e": time.time() + 60})
    assert access.unsign(code, "code")["h"] == "deals.machaka.net"
    assert access.unsign(code, "session") is None                                  # a code isn't a cookie
    assert access.unsign(code[:-3] + "AAA", "code") is None                        # changed: refused
    assert access.unsign(access.sign({"k": "code", "e": time.time() - 1}), "code") is None    # expired
    assert access.unsign("", "code") is None and access.unsign("a.b.c", "code") is None
    for bad in ["//evil.example/", "/\\evil.example", "https://evil.example/", "/a\r\nSet-Cookie: x=1", "/é", "x"]:
        assert access.safe_next(bad) == "/", bad
    assert access.safe_next("/v/abc?x=1") == "/v/abc?x=1"


def test_bridge_flow(monkeypatch):
    """A public name not in Access: off to lab.machaka.net's login, back with a code, then a cookie for that name."""
    import time
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from app import access

    def fake_check(token):          # stands in for Cloudflare's signature: "good" is a valid login
        if token == "good":
            access._good[token] = time.time() + 3600
            return True
        return False

    monkeypatch.setattr(access, "DEV", False)
    monkeypatch.setattr(access, "BRIDGE", "lab.machaka.net")
    monkeypatch.setattr(access, "check", fake_check)
    gate = access.AccessGate(Starlette(routes=[Route("/{p:path}", lambda r: PlainTextResponse("page " + r.url.path))]))

    def client(host):
        return TestClient(gate, base_url=f"https://{host}", follow_redirects=False)

    site, lab, other = client("tastematch.machaka.net"), client("lab.machaka.net"), client("deals.machaka.net")
    r = site.get("/v/abc?x=1")
    assert r.status_code == 302 and r.headers["location"].startswith("https://lab.machaka.net/lab-sso/start?")
    start = r.headers["location"].removeprefix("https://lab.machaka.net")
    assert lab.get(start).status_code == 403                                      # the bridge needs the login
    r = lab.get(start, headers={"cf-access-jwt-assertion": "good"})
    assert r.status_code == 302 and r.headers["location"].startswith("https://tastematch.machaka.net/lab-sso/finish?")
    finish = r.headers["location"].removeprefix("https://tastematch.machaka.net")
    assert other.get(finish).status_code == 403                                   # the code is for one name only
    r = site.get(finish)
    assert r.status_code == 302 and r.headers["location"] == "/v/abc?x=1"
    assert "lab_session=" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    used = TestClient(gate, base_url="https://tastematch.machaka.net", follow_redirects=False).get(finish)
    assert used.status_code == 403 and b"Open the page again" in used.content    # works once
    assert site.get(finish).headers["location"] == "/"                            # a reload, signed in: the page
    assert site.get("/v/abc").text == "page /v/abc"                               # the cookie opens the name...
    cookie = site.cookies.get("lab_session")
    assert other.get("/", headers={"cookie": f"lab_session={cookie}"}).status_code == 302    # ...and no other
    assert site.post("/v/abc", headers={"cookie": "lab_session=x"}).status_code == 403
    r = lab.get("/lab-sso/start?host=evil.example&next=/", headers={"cf-access-jwt-assertion": "good"})
    assert r.status_code == 403                                                   # only our names get codes
    monkeypatch.setattr(access, "BRIDGE", "")
    assert client("catcam.machaka.net").get("/").status_code == 403              # no bridge: refused, as before


def test_scope_names():
    from app.sources.bgt import _and
    assert [_and([]), _and(["a"]), _and(["a", "b"]), _and(["a", "b", "c"])] == ["", "a", "a and b", "a, b and c"]
