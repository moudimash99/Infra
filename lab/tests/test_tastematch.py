"""Taste Match: the match's words, what the lists pick, the child process that runs score.py, the page pieces.
Run from Infra/lab: PYTHONPATH=. python -m pytest -q tests/test_tastematch.py"""
import sqlite3

from app.sources import taste


def host(rid, match, n, points, rres=None, low=None, high=None):
    r = {"id": rid, "name": rid.split(":")[1].capitalize(), "match": match, "n": n, "low": low if low is not None else match - .1,
         "high": high if high is not None else match + .1, "points": points, "rres": rres or {}, "ranked": n >= taste.MIN_SHARED}
    r["label"], r["badge"] = taste.label(r)
    return r


def result(hosts, user, ures=None):
    ranked = [h for h in hosts if h["ranked"]]
    return {"user": user, "ures": ures or {}, "reviewers": hosts, "ranked": ranked, "by_id": {h["id"]: h for h in hosts}}


def test_match_words():
    assert taste.label({"n": 3, "match": 0.9})[0] == "Too few shared games"
    words = [taste.label({"n": 20, "match": m})[0] for m in (0.42, 0.2, 0.07, 0.0, -0.1, -0.3)]
    assert words == ["Strong match", "Good match", "Slight match", "No clear pattern", "Slightly different", "Different taste"]
    assert all(w in taste.SHORT for w in words)
    assert taste.nice_name("paul") == "Paul" and taste.nice_name("J-Rex") == "J-Rex"


def test_agree_disagree_loves():
    # user residuals and the host's: same sign = agree, opposite = differ; 30 is unrated by the user
    s = result([], {10: (8, 7, 1), 20: (4, 7, 1)}, ures={10: 1.0, 20: -1.5})
    r = host("susd:paul", 0.4, 12, {10: (5, 3.5, 1), 20: (5, 3.5, 1), 30: (4, 3.8, 1), 40: (2, 3.5, 1)},
             rres={10: 0.8, 20: 0.9, 30: 0.5, 40: -1.0})
    agree, differ, loves = taste.shared_lists(s, r)
    assert agree == [10] and differ == [20] and loves == [30]


def test_scores_run_score_py_read_only(tmp_path, monkeypatch):
    """The child process imports score.py from the pipeline's folder and gets a connection that can't write."""
    folder = tmp_path / "bgt"
    folder.mkdir()
    sqlite3.connect(folder / "bgtaste.db").execute("CREATE TABLE t (x)").connection.commit()
    (folder / "score.py").write_text(
        "def score_all(conn, user):\n"
        "    try:\n"
        "        conn.execute('INSERT INTO t VALUES (1)')\n"
        "        wrote = True\n"
        "    except Exception:\n"
        "        wrote = False\n"
        "    r = {'id': 'susd:paul', 'name': 'paul', 'n': 9 if not wrote else 0, 'rated': 20, 'corr': .5, 'match': .3,\n"
        "         'low': .1, 'high': .4, 'ures': {7: .5}, 'rres': {7: .4}, 'points': {7: (5, 7.1, 1.0)}}\n"
        "    return [r], {7: (8, 7.1, 1.0)}\n", encoding="utf-8")
    monkeypatch.setattr(taste, "BGT_DIR", str(folder))
    data = taste._compute()
    r = data["by_id"]["susd:paul"]
    assert r["n"] == 9, "score.py could write to the database"
    assert r["name"] == "Paul" and r["ranked"] and r["rank"] == 1 and r["label"] == "Strong match"
    assert data["user"] == {7: (8, 7.1, 1.0)} and r["points"][7] == (5, 7.1, 1.0) and data["ures"] == {7: .5}


def test_scores_keep_the_last_good_result(monkeypatch):
    s = taste._Scores()
    monkeypatch.setattr(taste, "_compute", lambda: {"ok": 1})
    assert s.get().data == {"ok": 1}

    def broken():
        raise RuntimeError("score.py failed")
    monkeypatch.setattr(taste, "_compute", broken)
    s._run()
    got = s.get()
    assert not got.ok and got.data == {"ok": 1} and "score.py failed" in got.error


def test_failure_lines():
    from app.site_bgt import FAIL_LINE
    m = FAIL_LINE.search("[40/44] FAIL YNwBu6hcygg Everdell Spirecrest (ERROR: unable to download video data: HTTP Error 403: Forbidden)")
    assert m.group(1) == "YNwBu6hcygg" and m.group(2) == "Everdell Spirecrest" and "403" in m.group(3)
    assert not FAIL_LINE.search("5 failures in a row: stopping so YouTube doesn't flag us; rerun later.")


def test_pieces():
    from app.web import templates
    t = templates.env.from_string('{% from "tastematch/_tm.html" import signed, meter, vlen with context %}'
                                  '{{ signed(0.42) }}|{{ signed(-0.07) }}|{{ vlen(65) }}|{{ vlen(3725) }}|{{ meter(0.25, 0, 0.5) }}')
    out = t.render(R="")
    assert out.startswith("+0.42|−0.07|1:05|1:02:05|")
    assert 'left:40%' in out and 'left:60.0%' in out and 'left:40.0%;width:40.0%' in out


def test_sorter_state_from_its_logs(tmp_path):
    from app.sources import bgt
    assert bgt.sorter_state(str(tmp_path)) is None
    (tmp_path / "sort_retry.log").write_text("=== 2026-11-23T20:58:41Z ttgg\n  3315/3315 labelled\n=== 2026-11-23T21:33:10Z bgco\n")
    (tmp_path / "sort_retry.bgco.out").write_text("Labelling 3805 videos from titles\n  660/3805 labelled\n  720/3805 labelled\n")
    s = bgt.sorter_state(str(tmp_path), {"bgco": "Board Game Co"})
    assert s["state"] == "running" and s["name"] == "Board Game Co" and s["pos"] == (720, 3805) and s["updated"]
    (tmp_path / "sort_retry.log").write_text("=== 2026-11-23T21:33:10Z bgco\nRuntimeError: limit\n"
                                             "=== 2026-11-23T23:00:00Z STOPPED at bgco (exit 1)\n")
    assert bgt.sorter_state(str(tmp_path))["state"] == "stopped"
    (tmp_path / "sort_retry_loop.log").write_text("=== 2026-11-23T23:01:00Z last pass: === 2026-11-23T23:00:00Z "
                                                  "STOPPED at bgco (exit 1); waiting 30 min\n")
    s = bgt.sorter_state(str(tmp_path))
    assert s["state"] == "waiting" and s["until"] == bgt._log_time("=== 2026-11-23T23:01:00Z") + 1800
    (tmp_path / "sort_retry_loop.log").write_text("=== 2026-11-24T06:00:00Z ALL DONE\n")
    assert bgt.sorter_state(str(tmp_path))["state"] == "done"


def test_sorting_counts_videos_and_hours(tmp_path, monkeypatch):
    from app.sources import bgt
    conn = sqlite3.connect(tmp_path / "bgtaste.db")
    conn.execute("CREATE TABLE videos (id, channel, duration, llm_category, category_override)")
    conn.executemany("INSERT INTO videos VALUES (?, ?, ?, ?, ?)", [
        ("a", "dt", 3600, "review", None), ("b", "dt", 7200, None, None), ("c", "susd", 1800, None, "other"),
        ("d", "susd", 1800, "review", None)])
    conn.commit()
    monkeypatch.setattr(bgt, "BGT_DIR", str(tmp_path))
    monkeypatch.setattr(bgt.channel_names, "get", lambda: type("R", (), {"data": {"dt": "Dice Tower"}})())
    data = bgt.sorting.fn()
    assert data["totals"] == {"videos": 4, "hours": 4.0, "sorted": 3, "sorted_h": 2.0}
    assert [(c["name"], c["sorted_h"], c["hours"]) for c in data["left"]] == [("Dice Tower", 1.0, 3.0)]


def test_pipeline_page_shows_hours_and_sorting(tmp_path, monkeypatch):
    import time as _time
    from starlette.testclient import TestClient
    from app import site_bgt, store
    from app.sources import Reading, bgt

    class Fixed:
        def __init__(self, data):
            self.data = data

        def get(self, *args):
            return Reading(True, self.data, "", _time.time())

    monkeypatch.setattr(store, "LAB_DB", str(tmp_path / "lab.db"))
    store.init()
    stages = {"audio": 900, "text": 700, "speakers": 200, "rated": 650, "voices": 100}
    ch = {"key": "susd", "name": "Shut Up & Sit Down", "videos": 1000, "hours": 728.0, "done": False, "started": True,
          "later": 0, "hours_to_transcribe": 200.0, "to_download": 100, **stages,
          **{k + "_h": v * 0.5 for k, v in stages.items()}}
    totals = {**stages, **{k + "_h": v * 0.5 for k, v in stages.items()}, "videos": 1000, "hours": 728.0,
              "hours_to_transcribe": 200.0, "hours_to_download": 50.0, "to_download": 100, "unavailable": 0, "later": 0}
    monkeypatch.setattr(bgt, "progress", Fixed({"channels": {"susd": ch}, "totals": totals, "audio_files": 900,
        "speed": {"downloads_24h": 3, "audio_hours_downloaded_24h": 139, "transcribed_24h": 1, "audio_hours_transcribed_24h": 18,
                  "speakers_hours_24h": 0, "rated_hours_24h": 0, "last_rated": _time.time() - 12 * 86400},
        "disk": {"free_gb": 500, "guard_gb": 150, "room_gb": 350, "gb_per_hour": 0.05, "room_hours": 7000},
        "scope": {"full": "Shut Up & Sit Down", "n_full": 1, "n_limited": 0}}))
    monkeypatch.setattr(bgt, "now", Fixed({"runs": [], "cron": {}, "youtube_blocked": False, "gpu_paused": False}))
    monkeypatch.setattr(bgt, "activity", Fixed([]))
    monkeypatch.setattr(bgt, "problems", Fixed({"unmatched": [], "n_unmatched": 0, "waiting_rating": 5}))
    client = TestClient(site_bgt.app)
    for sorter, words in [({"state": "running", "name": "Dice Tower", "pos": (720, 3805), "updated": _time.time() - 60},
                           "Sorting Dice Tower: 720 of 3,805 titles"),
                          ({"state": "waiting", "until": _time.time() + 600}, "starts again at"),
                          ({"state": "stopped", "name": "Dice Tower", "at": _time.time()}, "nothing restarts it"),
                          ({"state": "gave_up", "at": _time.time()}, "Gave up"), (None, "Sorting titles")]:
        monkeypatch.setattr(bgt, "sorting", Fixed({
            "totals": {"videos": 73923, "hours": 23436.0, "sorted": 37893, "sorted_h": 11966.0},
            "left": [{"key": "dt", "name": "Dice Tower", "videos": 23596, "hours": 6000.0, "sorted": 642, "sorted_h": 150.0}],
            "sorter": sorter}))
        r = client.get("/pipeline")
        assert r.status_code == 200 and "Traceback" not in r.text
        assert words in r.text
        assert "11,966 of 23,436 h" in r.text and "36,030 videos" in r.text
    assert "450 of 728 h" in r.text            # Downloaded: 900 videos at half an hour each, in hours
    assert "900 of 1,000 videos" in r.text and "325 h" in r.text   # Rated, per channel, in hours
    assert "278 h left" in r.text and "≈ 2.0 days" in r.text        # Downloaded: 728 - 450 = 278 h at 139 h a day
    assert "not moving" in r.text and "(last rating " in r.text


def test_eta():
    from app.sources import bgt
    assert bgt.eta(0, 5) is None and bgt.eta(100, 0) == "stalled" and bgt.eta(100, 50) == 2


def test_common_games_and_distribution():
    s = result([], {10: (8, 7, 1), 20: (4, 7, 1), 30: (6, 6, 1)}, ures={10: 1.0, 20: -1.5, 30: 0.2})
    r = host("susd:paul", 0.4, 3, {10: (5, 3.5, 1), 20: (5, 3.5, 0.4), 30: (3, 3, 1), 40: (2, 3.5, 1)},
             rres={10: 0.8, 20: 0.9, 30: -0.5, 40: -1.0})
    rows = taste.common_games(s, r, "agree", {10: {"name": "Brass"}})
    assert [x["gid"] for x in rows] == [10, 30, 20] and rows[0]["name"] == "Brass" and rows[0]["you"] == 4
    assert [x["gid"] for x in taste.common_games(s, r, "differ")] == [20, 30, 10]
    assert [x["gid"] for x in taste.common_games(s, r, "them")][0] in (10, 20)
    d = taste.distribution([5, 5, 4.5, 1, None, 3.4])
    assert [b["n"] for b in d] == [1, 0, 1, 0, 3] and d[4]["pct"] == 60 and d[4]["bar"] == 100


def test_reviewer_page(monkeypatch):
    import time as _time
    from starlette.testclient import TestClient
    from app import site_bgt
    from app.sources import Reading, bgt

    class Fixed:
        def __init__(self, data):
            self.data = data

        def get(self, *args):
            return Reading(True, self.data, "", _time.time())

    r = host("susd:paul", 0.27, 6, {g: (5 if g % 2 else 2, 3.5, 1 if g != 3 else 0.4) for g in range(1, 8)},
             rres={g: (0.6 if g % 2 else -0.4) for g in range(1, 8)})
    r.update(corr=0.85, counts=0.32, evidence=4.7, rated=7, channel="susd", channel_name="SU&SD",
             sources={3: [{"from": "team", "kind": "review", "confidence": "high", "score": 2, "video": "v3", "member": 0.8}],
                      1: [{"from": "own", "kind": "review", "confidence": "high", "score": 5, "video": "v1"}]})
    s = result([r], {g: (8 if g % 2 else 4, 7, 1) for g in range(1, 7)}, ures={g: (1.0 if g % 2 else -1.0) for g in range(1, 7)})
    s["at"] = _time.time()
    monkeypatch.setattr(taste, "scores", Fixed(s))
    monkeypatch.setattr(taste, "host_verdicts", Fixed({"host": {"channel": "susd", "name": "paul"}, "verdicts": [
        {"score": 5, "game": "Brass", "game_name": "Brass", "gid": 1, "video_id": "v1", "title": "Brass review", "quote": "great"},
        {"score": 5, "game": "Ark", "game_name": "Ark", "gid": 2, "video_id": "v2", "title": "Ark review", "quote": ""},
        {"score": 2, "game": "Monopoly", "game_name": "Monopoly", "gid": 3, "video_id": "v3", "title": "x", "quote": ""}]}))
    monkeypatch.setattr(taste, "things", Fixed({1: {"name": "Brass", "average": 8, "year": 2018}}))
    monkeypatch.setattr(bgt, "channel_info", Fixed({"susd": {"name": "Shut Up & Sit Down", "url": ""}}))
    client = TestClient(site_bgt.app)
    for sort in ("agree", "differ", "you", "them", "name", "bogus"):
        t = client.get(f"/reviewers/susd:paul?sort={sort}").text
        assert "Traceback" not in t and "Games you have in common" in t and "How they score" in t
    assert "32%" in t and "+0.85" in t and "Same way" in t and "Opposite" in t and "counts 40%" not in t
    assert "team verdict (Paul isn" in t                       # a lent team verdict says so, in words
    assert "67%" in t                      # two of the three verdicts are 5s


def test_grouping_by_game_and_host():
    vs = [{"gid": 1, "game": "Brass", "game_name": "Brass", "score": 5, "quote": "", "video_id": "a", "title": "Review"},
          {"gid": 1, "game": "Brass", "game_name": "Brass", "score": 4, "quote": "top", "video_id": "b", "title": "Top 10"},
          {"gid": 2, "game": "Ark", "game_name": "Ark", "score": 3, "quote": "", "video_id": "c", "title": "Ark"}]
    g = taste.by_game(vs)
    assert [x["gid"] for x in g] == [1, 2] and g[0]["score"] == 4.5 and g[0]["changed"] and g[0]["quote"] == "top"
    hv = [{"rid": "hc:ed", "host": "Edward", "score": 5, "video_id": "a"}, {"rid": "hc:ed", "host": "Edward", "score": 5, "video_id": "b"},
          {"rid": "hc:unknown", "host": "unknown 2", "score": 3, "video_id": "c"}, {"rid": "bgco:unknown", "host": "Unknown", "score": 2, "video_id": "d"}]
    h = taste.by_host(hv)
    assert len(h) == 2 and h[0]["host"] == "Edward" and len(h[0]["items"]) == 2 and not h[0]["changed"]
    assert h[1]["unnamed"] and h[1]["host"] == "Unnamed speaker" and len(h[1]["items"]) == 2
    notes = taste.source_notes([{"from": "team", "kind": "review", "confidence": "high", "score": 5, "video": "x"},
                                {"from": "own", "kind": "top list", "confidence": "low", "score": 4, "video": "y"}], "Paul", "SU&SD")
    assert notes[0]["text"] == "SU&SD's team verdict (Paul isn't in this video)" and not notes[0]["plain"]
    assert notes[1]["text"] == "in a top list, said without much certainty"
