"""Taste Match's deeper views, made from a match result (taste.build) and BGG's facts in bgtaste.db (read-only).
Every number they return is on the site's one scale, points out of 5 (BGG's 10-point ratings are halved).

- taste_profile(s): the categories, designers and mechanics a person rates above or below what BGG's average
  predicts for them, shrunk toward 0 when they rated few such games (LEAN_K), shown from MIN_GAMES games.
- likely(s, gid) / for_you(s, ...): what BGG's average and the person's taste profile suggest for a game they
  haven't rated, with the reasons (the traits they lean toward, the matched hosts who loved it).
  BGG's average counts for less when few people rated the game: it is pulled toward a typical game's
  average as if BGG_PRIOR more people had rated it typically (a new game rated 9.0 by its 300 backers counts as
  about 8.5). Measured on the owner's 143 ratings (2026-11-24, leaving one game out at a time): off by 0.58 points
  out of 5 on average, against 0.59 for BGG's average alone; the matched hosts' scores made it worse (0.60 to
  0.72), so they are shown as reasons and never mixed into the number.
- discover(s): across named hosts' own verdicts, games they score above (hidden gems) or below (less than the
  hype) what their usual line from BGG's average expects, and where they disagree most (divisive).
- host_profile(r): the taste profile of one host, from their verdicts.
"""
import statistics

from . import source
from .bgt import db

KINDS = {"boardgamecategory": "category", "boardgamedesigner": "designer", "boardgamemechanic": "mechanic"}
NOT_A_TRAIT = {"Expansion for Base-game", "(Uncredited)"}     # BGG links that say nothing about taste
LEAN_K = 3           # a trait's lean = sum of residuals / (games + LEAN_K): few games pull it toward 0
MIN_GAMES = 3        # a trait counts from this many rated games with it
BGG_PRIOR = 100      # ratings' weight of a typical game's average mixed into each BGG average (measured above)
SHOWN = 0.075        # leans smaller than this (points out of 5) are left out of profiles and reasons
WEIGHT_BANDS = {"light": (0, 2.2), "medium": (2.2, 3.3), "heavy": (3.3, 6)}


@source("insights.facts", ttl=3600)
def facts():
    """{"games": bgg id -> {name, year, average, n (BGG ratings), weight, min/max players, time, expansion, links},
    "typical": the median BGG average of games with 100+ ratings}."""
    conn = db()
    try:
        out = {r["id"]: {"name": r["name"], "year": r["year"], "average": r["average"], "n": r["users_rated"] or 0,
                         "weight": r["average_weight"], "min_p": r["min_players"], "max_p": r["max_players"],
                         "time": r["playing_time"], "expansion": r["type"] == "boardgameexpansion", "links": set()}
               for r in conn.execute("""SELECT id, type, name, year, average, users_rated, average_weight,
                                        min_players, max_players, playing_time FROM bgg_things WHERE average > 0""")}
        marks = ",".join("?" * len(KINDS))
        for r in conn.execute(f"SELECT id, link_type, target_name FROM bgg_links WHERE link_type IN ({marks})",
                              list(KINDS)):
            if r["id"] in out and r["target_name"] not in NOT_A_TRAIT:
                out[r["id"]]["links"].add((KINDS[r["link_type"]], r["target_name"]))
    finally:
        conn.close()
    known = [g["average"] for g in out.values() if g["n"] >= 100]
    return {"games": out, "typical": statistics.median(known) if known else 7.0}


def _facts():
    return facts.get().data or {"games": {}, "typical": 7.0}


def bgg_average(g, typical):
    """A game's BGG average (out of 10), pulled toward a typical game's when few people rated it."""
    return (g["average"] * g["n"] + typical * BGG_PRIOR) / (g["n"] + BGG_PRIOR)


def _fit(points):
    """The person's line from BGG's average to their rating (as score.fit_residuals): avg -> expected rating."""
    w = sum(p[2] for p in points.values())
    if not w:
        return lambda a: a
    mx = sum(p[1] * p[2] for p in points.values()) / w
    my = sum(p[0] * p[2] for p in points.values()) / w
    sxx = sum(p[2] * (p[1] - mx) ** 2 for p in points.values())
    sxy = sum(p[2] * (p[1] - mx) * (p[0] - my) for p in points.values())
    b = sxy / sxx if len(points) >= 5 and sxx > 0 else 0.0
    return lambda a: my + b * (a - mx)


def _leans(residuals, f):
    """{(kind, name): {lean, n, games}} from {game: residual}: how far above or below expectation the person goes
    on games with that trait (in their residuals' units)."""
    acc = {}
    for g, res in residuals.items():
        for link in (f.get(g) or {}).get("links", ()):
            a = acc.setdefault(link, [0.0, 0, []])
            a[0] += res
            a[1] += 1
            a[2].append(g)
    return {k: {"lean": s / (n + LEAN_K), "n": n, "games": games} for k, (s, n, games) in acc.items()}


def _model(s):
    """For the person of match result s: (expect: BGG average out of 10 -> their rating out of 10, trait leans),
    with BGG averages pulled as in bgg_average. Kept on s, which is rebuilt whenever the match is."""
    if "_model" not in s:
        fx = _facts()
        f, typical = fx["games"], fx["typical"]
        pts = {g: (v[0], bgg_average(f[g], typical) if g in f else v[1], 1.0) for g, v in s["user"].items()}
        expect = _fit(pts)
        res = {g: p[0] - expect(p[1]) for g, p in pts.items()}
        s["_model"] = (expect, _leans(res, f))
    return s["_model"]


def _profile(leans, scale, top):
    out = {}
    for kind in KINDS.values():
        items = [{"name": name, **v, "lean": v["lean"] * scale} for (k, name), v in leans.items()
                 if k == kind and v["n"] >= MIN_GAMES]
        out[kind] = {"up": sorted((i for i in items if i["lean"] >= SHOWN), key=lambda i: -i["lean"])[:top],
                     "down": sorted((i for i in items if i["lean"] <= -SHOWN), key=lambda i: i["lean"])[:top]}
    return out


def taste_profile(s):
    """{kind: {"up": [...], "down": [...]}} for the person of match result s; each {name, lean (points out of 5),
    n (rated games with the trait), games}."""
    return _profile(_model(s)[1], 0.5, 6)


def host_profile(r):
    """The same for a host, from their verdicts (already out of 5)."""
    return _profile(_leans(r["rres"], _facts()["games"]), 1.0, 5)


def likely(s, gid):
    """What BGG's average and the person's taste profile suggest for a game, out of 5, with the traits behind it:
    {"value", "bgg" (out of 5, as pulled), "why": [{kind, name, lean, n}]}, or None for a game BGG doesn't know."""
    fx = _facts()
    x = fx["games"].get(gid)
    if not x:
        return None
    expect, leans = _model(s)
    feats = [(link, leans[link]) for link in x["links"] if link in leans and leans[link]["n"] >= MIN_GAMES]
    lean = sum(v["lean"] for _, v in feats) / len(feats) if feats else 0.0
    why = [{"kind": k, "name": n, "lean": v["lean"] / 2, "n": v["n"]}
           for (k, n), v in sorted(feats, key=lambda kv: -kv[1]["lean"]) if v["lean"] / 2 >= SHOWN][:3]
    bgg = bgg_average(x, fx["typical"])
    return {"value": max(1.0, min(5.0, (expect(bgg) + lean) / 2)), "bgg": bgg / 2, "why": why}


def _own(r, g):
    """Is host r's score on game g their own verdict (not only the channel's team verdict lent to them)?"""
    srcs = (r.get("sources") or {}).get(g)
    return not srcs or any(x.get("from") != "team" for x in srcs)


def _reviewed(s):
    """Games named hosts scored with their own verdicts: {gid: [(host, score, residual)]}. A team verdict lent to
    each regular host counts in the match, but here it would be one opinion repeated, so it is left out."""
    out = {}
    for r in s["reviewers"]:
        if (r.get("name") or "").lower().startswith(("unknown", "team")):
            continue
        for g, (score, _avg, _w) in r["points"].items():
            if _own(r, g):
                out.setdefault(g, []).append((r, score, r["rres"].get(g, 0.0)))
    return out


def reviewed_ids(s):
    """The games named hosts scored with their own verdicts."""
    return set(_reviewed(s))


def for_you(s, players=None, weight="", max_time=None, limit=24):
    """Reviewed games the person hasn't rated, best first by likely(), filtered by player count, weight band and
    play time. Each: {gid, name, likely, bgg, n (BGG ratings), why, fans: [(host, score)], hosts, loved, facts}."""
    f = _facts()["games"]
    lo, hi = WEIGHT_BANDS.get(weight, (0, 9))
    out = []
    for g, said in _reviewed(s).items():
        x = f.get(g)
        if not x or g in s["user"] or x["expansion"]:      # expansions: only worth it with the base game
            continue
        if players and not ((x["min_p"] or 0) <= players <= (x["max_p"] or 99)
                            or (players >= 6 and (x["max_p"] or 0) >= players)):      # "6+": six or more
            continue
        if weight and not (x["weight"] and lo <= x["weight"] < hi):
            continue
        if max_time and (x["time"] or 0) > max_time:
            continue
        lk = likely(s, g)
        fans = sorted(((r, sc) for r, sc, _ in said if r.get("ranked") and r["match"] > 0.05 and sc >= 4),
                      key=lambda t: -t[0]["match"])
        out.append({"gid": g, "name": x["name"], "likely": lk["value"], "bgg": lk["bgg"], "n": x["n"],
                    "why": lk["why"], "fans": fans, "hosts": len(said), "facts": x,
                    "loved": sum(1 for _, sc, _ in said if sc >= 4)})
    out.sort(key=lambda e: (-e["likely"], -e["hosts"]))
    return out[:limit]


def discover(s, min_hosts=3, limit=12):
    """Hidden gems, less than the hype, and divisive games, from named hosts' own verdicts on games with min_hosts
    or more of them. above: how far, in points out of 5, the hosts score the game above (or below) what their own
    line from BGG's average expects, averaged over them; spread: the spread of their scores."""
    f = _facts()["games"]
    rows = []
    for g, said in _reviewed(s).items():
        if len(said) < min_hosts or g not in f or f[g]["expansion"]:
            continue
        scores = [sc for _, sc, _ in said]
        rows.append({"gid": g, "name": f[g]["name"], "bgg": f[g]["average"] / 2, "n": f[g]["n"], "hosts": len(said),
                     "above": sum(res for _, _, res in said) / len(said), "spread": statistics.pstdev(scores),
                     "mean": sum(scores) / len(scores), "high": max(said, key=lambda t: t[1]),
                     "low": min(said, key=lambda t: t[1]), "facts": f[g]})
    return {"gems": sorted((r for r in rows if r["above"] >= 0.25), key=lambda r: -r["above"])[:limit],
            "overrated": sorted((r for r in rows if r["above"] <= -0.25), key=lambda r: r["above"])[:limit],
            "divisive": sorted((r for r in rows if r["spread"] >= 1.0), key=lambda r: -r["spread"])[:limit],
            "games": len(rows)}
