"""deals.machaka.net against a small BoardGameFinder folder built here: what counts as new and cheap, the games list,
the picture rule, and every page rendering. Run: PYTHONPATH=. python -m pytest -q tests/test_deals.py (from lab/)."""
import csv
import datetime as dt
import json
import sqlite3
import time

import pytest

from app.config import TZ
from app.sources import Reading, bgf

NOW = dt.datetime.now(TZ)


def paris(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")


class Fixed:
    """Stands in for a source: always this reading."""
    def __init__(self, data):
        self.data = data

    def get(self, *args):
        return Reading(True, self.data, "", time.time())


@pytest.fixture
def bgf_dir(tmp_path, monkeypatch):
    """Games.db with two games: Ark (BGG 1) with four live copies, Bundle Game (BGG 2) with a lot to judge."""
    (tmp_path / "Output").mkdir()
    db = sqlite3.connect(tmp_path / "Games.db")
    db.executescript("""
        CREATE TABLE BGG_Games (ID, Rank, Name, URL, French_Name, Year, Produced, Is_Legacy, Image_URL);
        CREATE TABLE Game_main_page_link (BG_Id, Link, Language, Name, Announcments, Automatic, Ignore, Verified,
                                          Is_Expansion, Image_URL);
        CREATE TABLE Post (BG_ID, Link, Price, Is_Online, Date_Last_Viewed, Ignore, Post_Date, Is_Extension, Is_Bundle,
                           Is_Special_Edition, Condition, Ad_Language, Can_Ship, Listing_Text, First_Seen, Alerted_At,
                           Postcode, Location, Seller_Description);
        CREATE TABLE Price_History (Link, Price, Date);
    """)
    db.execute("INSERT INTO BGG_Games VALUES (1, 12, 'Ark', 'https://boardgamegeek.com/boardgame/1', 'Arche', 2021, 1, 0, '')")
    db.execute("INSERT INTO BGG_Games VALUES (2, 300, 'Bundle Game', '', NULL, 2015, 1, 0, '')")
    for bg, name in ((1, "Ark"), (2, "Bundle Game"), (3, "Nothing For Sale")):
        db.execute("INSERT INTO Game_main_page_link VALUES (?, ?, 'English', ?, 0, 1, 0, 1, 0, '')",
                   (bg, f"https://www.okkazeo.com/jeux/view/{bg}/x", name))
    posts = [  # link, game, price, hours since first seen, bundle, extension
        ("a-cheap", 1, 20.0, 1, 0, 0), ("a-tiny-saving", 1, 34.0, 2, 0, 0), ("a-unvalued", 1, 40.0, 3, 0, 0),
        ("a-old", 1, 30.0, 60, 0, 0), ("a-parts", 1, 5.0, 100, 0, 1), ("b-lot", 2, 90.0, 5, 1, 0)]
    for link, bg, price, h, bundle, ext in posts:
        db.execute("""INSERT INTO Post (BG_ID, Link, Price, Is_Online, Ignore, Post_Date, Is_Extension, Is_Bundle,
                      Is_Special_Edition, Condition, Ad_Language, Can_Ship, Listing_Text, First_Seen, Location)
                      VALUES (?, ?, ?, 1, 0, ?, ?, ?, 0, 'etat_excellent', 'VF', 1, 'Jeu', ?, 'Toulouse (31000)\n01/11/2026')""",
                   (bg, f"https://www.okkazeo.com/annonces/{link}", price, paris(h)[:10] + " 00:00:00", ext, bundle, paris(h)))
    # a drop: 'a-unvalued' was 48 two checks ago
    db.execute("INSERT INTO Price_History VALUES ('https://www.okkazeo.com/annonces/a-unvalued', 48, ?)", (paris(50),))
    db.execute("INSERT INTO Price_History VALUES ('https://www.okkazeo.com/annonces/a-unvalued', 40, ?)", (paris(1),))
    db.execute("INSERT INTO Post (BG_ID, Link, Price, Is_Online, Ignore, Date_Last_Viewed, Condition, First_Seen) "
               "VALUES (1, 'https://www.okkazeo.com/annonces/a-sold', 26, 0, 0, ?, 'neuf_cello', ?)", (paris(30), paris(400)))
    db.commit()
    db.close()
    cols = ["Name", "Price", "Net", "Edge", "Baseline", "Sold_Comps", "Condition", "Language", "Bundle", "Ships", "Link",
            "Post_Date", "BG_ID"]
    rows = [["Ark", "20.00", "12.00", "20.00", "40.00", "3", "etat_excellent", "VF", "", "yes",
             "https://www.okkazeo.com/annonces/a-cheap", "2026-11-09", "1"],
            ["Ark", "34.00", "0.40", "6.00", "40.00", "3", "etat_excellent", "VF", "", "yes",
             "https://www.okkazeo.com/annonces/a-tiny-saving", "2026-11-09", "1"]]
    for name, keep in (("scored.csv", rows), ("deals.csv", rows[:1])):
        with open(tmp_path / "Output" / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(keep)
    (tmp_path / "Output" / "review_pending.json").write_text(json.dumps(
        [{"link": "https://www.okkazeo.com/annonces/b-lot", "price": 90, "bg_id": 2}]), encoding="utf-8")
    monkeypatch.setattr(bgf, "BGF_DIR", str(tmp_path))
    monkeypatch.setattr(bgf, "_bgg_picture", lambda bg_id: None)            # no call to BGG from tests
    for s in (bgf.scored, bgf.deals, bgf.new_listings, bgf.new_deals, bgf.drops, bgf.market, bgf.bundles, bgf.games,
              bgf.game, bgf.image_map):
        s._cache.clear()
    return tmp_path


def test_words():
    assert bgf.cond("etat_excellent") == "Excellent" and bgf.cond("abime") == "Damaged" and bgf.cond("") == ""
    assert bgf.edition("VF") == "French" and bgf.edition(None) == ""
    now = NOW.timestamp()
    assert bgf.age(now, now) == "today" and bgf.age(now - 86400, now) == "yesterday"
    assert bgf.age(now - 5 * 86400, now) == "5 days ago" and bgf.age(None) == ""
    assert bgf._place("Paris (75013)\n08/11/2026") == "Paris (75013)"


def test_picture_rule(monkeypatch):
    """Okkazeo pictures named after the game page's number are often another game's box: never shown."""
    monkeypatch.setattr(bgf, "_bgg_picture", lambda bg_id: None)
    monkeypatch.setattr(bgf, "image_map", Fixed({1: "https://www.okkazeo.com/images/jeux/5576.jpg",
                                                 2: "https://www.okkazeo.com/images/jeux/65e8782c41eab3.69816426.jpg"}))
    assert bgf.image_url(1) is None
    assert bgf.image_url(2).endswith("65e8782c41eab3.69816426.jpg")
    monkeypatch.setattr(bgf, "_bgg_picture", lambda bg_id: "https://cf.geekdo-images.com/x__original/img/y=/0x0/pic1.jpg")
    assert bgf.image_url(1).startswith("https://cf.geekdo-images.com/")       # BGG's first


def test_new_and_cheap(bgf_dir):
    day = bgf.new_listings.fn(1)
    assert [x["link"].rsplit("/", 1)[1] for x in day["cheap"]] == ["a-cheap"]          # a 0.40 € saving isn't one
    assert {x["link"].rsplit("/", 1)[1] for x in day["rest"]} == {"a-tiny-saving", "a-unvalued", "b-lot"}
    cheap = day["cheap"][0]
    assert cheap["net"] == 12 and cheap["saving_pct"] == 30 and cheap["place"] == "Toulouse (31000)"
    assert bgf.new_listings.fn(3)["total"] == 5                                        # the 60-hour-old one too


def test_new_before_scored_exists(bgf_dir):
    """Until BoardGameFinder writes scored.csv, everything new is listed, nothing called cheap."""
    (bgf_dir / "Output" / "scored.csv").unlink()
    bgf.scored._cache.clear()
    day = bgf.new_listings.fn(1)
    assert not day["available"] and day["cheap"] == [] and len(day["rest"]) == 4


def test_games_and_game(bgf_dir):
    games = {g["bg_id"]: g for g in bgf.games.fn()}
    ark = games[1]
    assert ark["live"] == 5 and ark["cheapest"] == 20 and ark["typical"] == 32       # the 5 € parts don't count
    assert ark["usual"] == 40 and ark["under"] == 38 and games[3]["live"] == 0
    g = bgf.game.fn(1)
    assert g["cheapest"] == 20 and g["usual"] == 40 and len(g["listings"]) == 5
    assert [x["price"] for x in g["gone"]] == [26]
    assert bgf.game.fn(999) is None


def test_bundles_and_drops(bgf_dir):
    b = bgf.bundles.fn()
    assert b["items"][0]["kind"] == "Bundle" and b["kinds"] == {"Bundle": 1}
    drops = bgf.drops.fn(3)
    assert len(drops) == 1 and drops[0]["before"] == 48 and drops[0]["off_pct"] == 17


def test_pages_render(bgf_dir, tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from app import site_deals, store, summary
    monkeypatch.setattr(store, "LAB_DB", str(tmp_path / "lab.db"))
    store.init()
    run = {"steps": [{"key": k, "label": label, "critical": c, "llm": k in bgf.LLM_STEPS, "failures": 0}
                     for k, label, c in bgf.STEPS],
           "detail": {"steps": [{"at": "2026-11-10 07:00:01", "label": "Scraping prices", "key": "price_finder",
                                 "failed": False, "code": None, "errors": []},
                                {"at": "2026-11-10 07:50:00", "label": "Classifying listings with the LLM",
                                 "key": "classify_postings_with_llm", "failed": True, "code": 1,
                                 "errors": ["fm: fm-llm unreachable at http://192.168.1.116:8792"]}]},
           "deal_log": [], "runs": [{"name": "boardgamefinder-1", "start": time.time() - 3600,
                                     "end": time.time() - 100, "state": "ok"}], "cron": None}
    monkeypatch.setattr(bgf, "run", Fixed(run))
    client = TestClient(site_deals.app)
    for path in ["/", "/new", "/new?days=7", "/drops", "/bundles", "/bundles?kind=Bundle", "/games",
                 "/games?q=ark&sort=price", "/games?show=all&sort=name&p=9", "/game/1", "/game/2", "/status"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Traceback" not in r.text and 'data-site="deals"' in r.text, path
    home = client.get("/").text
    assert "Best deals right now" in home and "Save 12 €" in home and "/img/1-m.jpg" in home
    assert "Waiting for fm-llm" in client.get("/status").text
    assert client.get("/game/999").status_code == 404
    assert client.get("/file/nope").status_code == 404
    tiles = summary.deals()
    assert tiles["deals"] == "1" and tiles["new"] == "1" and tiles["bundles"] == "1"
