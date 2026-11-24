"""Taste Match's deeper views, made from a match result (taste.build) and BGG's facts in bgtaste.db (read-only):

- taste_profile(s): the categories, designers and mechanics a person rates above or below what BGG's average
  predicts for them, shrunk toward 0 when they rated few such games (LEAN_K) and shown only from MIN_GAMES games.
- for_you(s, ...): unrated games with reviews, ranked by what BGG's average and the profile suggest for this person,
  with the reasons (the features they lean toward, the matched hosts who loved it). Measured on the owner's 143
  ratings (2026-11-24, leave one out): the profile beats BGG's average alone a little (mean error 1.16 vs 1.19 out
  of 10); the matched hosts' scores did not (1.20 to 1.43), so they are shown as evidence, never as a forecast.
- discover(s): across all hosts, games they like more than BGG does (hidden gems), less (overrated), and where they
  disagree most with each other (divisive).
- host_profile(s, r): the same profile for one host, from their verdicts.
"""
import statistics

from . import source
from .bgt import db

KINDS = {"boardgamecategory": "category", "boardgamedesigner": "designer", "boardgamemechanic": "mechanic"}
LEAN_K = 3           # a feature's lean = sum of residuals / (games + LEAN_K): few games pull it toward 0
MIN_GAMES = 3        # a feature is shown from this many rated games with it
WEIGHT_BANDS = {"light": (0, 2.2), "medium": (2.2, 3.3), "heavy": (3.3, 6)}


@source("insights.facts", ttl=3600)
def facts():
    """bgg id -> {name, year, average, weight, min/max players, time, links: {(kind, name)}} for every thing in the
    database with a BGG average."""
    conn = db()
    try:
        out = {r["id"]: {"name": r["name"], "year": r["year"], "average": r["average"], "weight": r["average_weight"],
                         "min_p": r["min_players"], "max_p": r["max_players"], "time": r["playing_time"], "links": set()}
               for r in conn.execute("""SELECT id, name, year, average, average_weight, min_players, max_players,
                                        playing_time FROM bgg_things WHERE average > 0""")}
        marks = ",".join("?" * len(KINDS))
        for r in conn.execute(f"SELECT id, link_type, target_name FROM bgg_links WHERE link_type IN ({marks})",
                              list(KINDS)):
            if r["id"] in out:
                out[r["id"]]["links"].add((KINDS[r["link_type"]], r["target_name"]))
    finally:
        conn.close()
    return out


def _fit(user):
    """The person's line from BGG's average to their rating (as score.fit_residuals): avg -> expected rating."""
    w = sum(p[2] for p in user.values())
    if not w:
        return lambda a: a
    mx = sum(p[1] * p[2] for p in user.values()) / w
    my = sum(p[0] * p[2] for p in user.values()) / w
    sxx = sum(p[2] * (p[1] - mx) ** 2 for p in user.values())
    sxy = sum(p[2] * (p[1] - mx) * (p[0] - my) for p in user.values())
    b = sxy / sxx if len(user) >= 5 and sxx > 0 else 0.0
    return lambda a: my + b * (a - mx)


def _leans(residuals, f):
    """{(kind, name): {lean, n, games}} from {game: residual}: how far above or below expectation the person goes
    on games with that feature."""
    acc = {}
    for g, res in residuals.items():
        for link in (f.get(g) or {}).get("links", ()):
            a = acc.setdefault(link, [0.0, 0, []])
            a[0] += res
            a[1] += 1
            a[2].append(g)
    return {k: {"lean": s / (n + LEAN_K), "n": n, "games": games} for k, (s, n, games) in acc.items()}


def taste_profile(s, scale=1.0):
    """{kind: {"up": [...], "down": [...]}} for the user of match result s; each item {name, lean (in points out of
    10 when scale=1), n, games}."""
    f = facts.get().data or {}
    out = {}
    leans = _leans(s["ures"], f)
    for kind in KINDS.values():
        items = [{"name": name, **v, "lean": v["lean"] * scale} for (k, name), v in leans.items()
                 if k == kind and v["n"] >= MIN_GAMES]
        out[kind] = {"up": sorted((i for i in items if i["lean"] > 0.15), key=lambda i: -i["lean"])[:6],
                     "down": sorted((i for i in items if i["lean"] < -0.15), key=lambda i: i["lean"])[:6]}
    return out


def host_profile(r):
    """The same for a host, from their verdicts (scores out of 5, shown doubled to compare with BGG's 10)."""
    f = facts.get().data or {}
    leans = _leans(r["rres"], f)
    out = {}
    for kind in KINDS.values():
        items = [{"name": name, **v, "lean": v["lean"] * 2} for (k, name), v in leans.items()
                 if k == kind and v["n"] >= MIN_GAMES]
        out[kind] = {"up": sorted((i for i in items if i["lean"] > 0.15), key=lambda i: -i["lean"])[:5],
                     "down": sorted((i for i in items if i["lean"] < -0.15), key=lambda i: i["lean"])[:5]}
    return out


def _reviewed(s):
    """Games any host scored: {gid: [(host, score, residual z)]}."""
    out = {}
    for r in s["reviewers"]:
        res = r["rres"]
        sd = statistics.pstdev(res.values()) if len(res) > 2 else 0
        for g, (score, _avg, _w) in r["points"].items():
            out.setdefault(g, []).append((r, score, (res[g] / sd) if sd else 0.0))
    return out


def for_you(s, players=None, weight="", max_time=None, limit=24):
    """Unrated games with reviews, ranked by what BGG's average and the person's taste profile suggest, filtered by
    player count, weight band and play time. Each: {gid, name, expected (out of 10), bgg, why: [features],
    fans: [(host, score)], hosts: count, facts}."""
    f = facts.get().data or {}
    expect = _fit(s["user"])
    leans = _leans(s["ures"], f)
    lo, hi = WEIGHT_BANDS.get(weight, (0, 9))
    out = []
    for g, said in _reviewed(s).items():
        x = f.get(g)
        if not x or g in s["user"]:
            continue
        if players and not ((x["min_p"] or 0) <= players <= (x["max_p"] or 99)):
            continue
        if weight and not (x["weight"] and lo <= x["weight"] < hi):
            continue
        if max_time and (x["time"] or 0) > max_time:
            continue
        feats = [(link, leans[link]) for link in x["links"] if link in leans and leans[link]["n"] >= MIN_GAMES]
        lean = sum(v["lean"] for _, v in feats) / len(feats) if feats else 0.0
        why = [{"kind": k, "name": n, "lean": v["lean"], "n": v["n"]}
               for (k, n), v in sorted(feats, key=lambda kv: -kv[1]["lean"]) if v["lean"] > 0.15][:3]
        fans = sorted(((r, sc) for r, sc, _z in said if r.get("ranked") and r["match"] > 0.05 and sc >= 4),
                      key=lambda t: -t[0]["match"])
        out.append({"gid": g, "name": x["name"], "expected": expect(x["average"]) + lean, "bgg": x["average"],
                    "why": why, "fans": fans, "hosts": len(said), "facts": x,
                    "loved": sum(1 for _, sc, _z in said if sc >= 4)})
    out.sort(key=lambda e: (-e["expected"], -e["hosts"]))
    return out[:limit]


def discover(s, min_hosts=3, limit=12):
    """Hidden gems, overrated and divisive games across all hosts, from their scores relative to their own usual
    against BGG's average (z), and the spread of their raw scores."""
    f = facts.get().data or {}
    rows = []
    for g, said in _reviewed(s).items():
        if len(said) < min_hosts or g not in f:
            continue
        zs = [z for _, _, z in said]
        scores = [sc for _, sc, _ in said]
        rows.append({"gid": g, "name": f[g]["name"], "bgg": f[g]["average"], "hosts": len(said),
                     "z": sum(zs) / len(zs), "spread": statistics.pstdev(scores), "mean": sum(scores) / len(scores),
                     "high": max(said, key=lambda t: t[1]), "low": min(said, key=lambda t: t[1]), "facts": f[g]})
    return {"gems": sorted((r for r in rows if r["z"] > 0.3), key=lambda r: -r["z"])[:limit],
            "overrated": sorted((r for r in rows if r["z"] < -0.3), key=lambda r: r["z"])[:limit],
            "divisive": sorted((r for r in rows if r["spread"] >= 1.0), key=lambda r: -r["spread"])[:limit],
            "games": len(rows)}
