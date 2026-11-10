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


def test_games_your_matches_love():
    user = {1: (8, 7, 1)}
    close = host("susd:paul", 0.4, 12, {1: (5, 3.5, 1), 2: (5, 3.5, 1), 3: (4, 3.5, 1), 4: (3, 3.5, 1)})
    far = host("hc:tony", -0.3, 12, {2: (5, 3.5, 1), 5: (5, 3.5, 1)})           # doesn't match: ignored
    thin = host("npi:elaine", 0.9, 2, {6: (5, 3.5, 1)})                          # too few shared: ignored
    s = result([close, far, thin], user)
    recs = taste.recommendations(s)
    assert [e["gid"] for e in recs] == [2, 3]                                     # 5 beats 4; rated, scored 3: out
    assert recs[0]["fans"][0][0]["id"] == "susd:paul" and recs[0]["score"] == 5
    assert [e["gid"] for e in taste.recommendations(s, skip={2})] == [3]         # owned games are skipped
    assert taste.matches_say(s, 2) == 5 and taste.matches_say(s, 5) is None


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
