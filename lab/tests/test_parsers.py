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


def test_sites_under_lab_paths():
    """lab.machaka.net/deals/... reaches the deals site, and its nav links stay on that name."""
    from starlette.testclient import TestClient
    from app import main, web
    inner = TestClient(main.app.app, base_url="https://lab.machaka.net")     # past the login check
    assert inner.get("/deals/static/lab.css").status_code == 200
    assert inner.get("/tastematch/static/lab.js").status_code == 200
    assert inner.get("/nothing/").status_code == 404
    base = web.templates.get_template("base.html")
    under = base.render(R="/catcam", me="catcam")
    assert 'href="/tastematch/"' in under and 'href="/deals/"' in under and 'href="/catcam/"' in under
    own = base.render(R="", me="deals")
    assert 'href="https://tastematch.machaka.net"' in own and 'href="/"' in own


def test_scope_names():
    from app.sources.bgt import _and
    assert [_and([]), _and(["a"]), _and(["a", "b"]), _and(["a", "b", "c"])] == ["", "a", "a and b", "a, b and c"]
