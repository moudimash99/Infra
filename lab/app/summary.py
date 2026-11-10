"""Short numbers for Homepage's tiles on lab.machaka.net (its customapi widget reads these, inside the cluster)."""
import time

from .fmt import ago
from .sources import bgf, bgt


def _pct(part, whole):
    return f"{round(100 * part / whole)}%" if whole else "-"


def taste_match():
    prog, now = bgt.progress.get(), bgt.now.get()
    out = {"now": "-", "downloaded": "-", "transcribed": "-", "rated": "-"}
    if now.ok and now.data:
        run = next((r for r in now.data["runs"] if r["state"] == "running"), None)
        st = run["log"]["now"] if run else None
        if st:
            pos = f" {st['pos'][0]}/{st['pos'][1]}" if st.get("pos") else ""
            out["now"] = f"{st.get('label') or st['what']}{pos}"
        elif now.data["youtube_blocked"]:
            out["now"] = "YouTube blocking downloads"
        else:
            out["now"] = "Idle" + (" (GPU paused)" if now.data["gpu_paused"] else "")
    if prog.ok and prog.data:
        t = prog.data["totals"]
        out.update(downloaded=_pct(t["audio"], t["videos"]), transcribed=_pct(t["text"], t["videos"]),
                   rated=_pct(t["rated"], t["videos"]))
    return out


def deals():
    d, fresh, b, m = bgf.deals.get(), bgf.new_deals.get(), bgf.bundles.get(), bgf.market.get()
    out = {"best": "-", "deals": "-", "new": "-", "bundles": "-"}
    if d.ok and d.data and d.data["deals"]:
        top = d.data["deals"][0]
        out["best"] = f"{top['name'][:22]} {top['price']:.0f} € (−{top['net']:.0f})"
        out["deals"] = str(len(d.data["deals"]))
    if fresh.ok and fresh.data:
        out["new"] = str(len(fresh.data["deals"])) if fresh.data["available"] else "from tomorrow"
    if b.ok and b.data:
        out["bundles"] = str(len(b.data["items"]))
    if m.ok and m.data and (m.data["live"] == 0 or (m.data["last_scrape"] and time.time() - m.data["last_scrape"] > 30 * 3600)):
        out["best"] = "last scrape failed"
    return out


def catcam():
    from .site_cats import flashes
    from .sources import cats
    out = {"flash": "first one tonight", "weighed": "-"}
    f = flashes(1)
    if f:
        out["flash"] = f"{f[0]['day'][5:]}: {f[0].get('moments', 0)} moments, {round(f[0].get('seconds', 0))} s"
    w = cats.weights.get()
    if w.ok and w.data:
        out["weighed"] = ago(max(p[0] for pts in w.data.values() for p in pts))
    return out
