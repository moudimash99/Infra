"""Small sources: the sites, the mail classifier, and the PC's reports."""
import datetime as dt
import time

import httpx

from . import prom, source
from .. import store
from ..config import MAIL_CLASSIFIER, PC_STALE_MINUTES, SITES, TZ

_http = httpx.Client(timeout=6, follow_redirects=False)


@source("sites", ttl=60)
def sites():
    out = []
    for name, public, internal in SITES:
        if not internal:
            out.append({"name": name, "url": public, "up": None, "ms": None, "code": None})
            continue
        t = time.time()
        try:
            r = _http.get(internal)
            out.append({"name": name, "url": public, "up": r.status_code < 500, "code": r.status_code,
                        "ms": round((time.time() - t) * 1000)})
        except httpx.HTTPError as e:
            out.append({"name": name, "url": public, "up": False, "code": None, "ms": None,
                        "error": type(e).__name__})
    return out


@source("mail", ttl=60)
def mail_classifier():
    """The model it runs, and emails classified by decision today and over 7 days (Prometheus scrapes its counter,
    so a pod restart doesn't reset these)."""
    health = _http.get(f"{MAIL_CLASSIFIER}/health").json()
    midnight = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = max(1, int((dt.datetime.now(TZ) - midnight).total_seconds() // 60))
    q = "sum by (decision) (increase(mail_classifier_emails_total[{}]))"
    today = {m.get("decision", "?"): round(v) for m, v in prom.query(q.format(f"{minutes}m"))}
    week = {m.get("decision", "?"): round(v) for m, v in prom.query(q.format("7d"))}
    return {"health": health, "today": today, "week": week}


@source("pc", ttl=30)
def pc():
    """The PC's latest report (it posts every 10 minutes) and whether it's fresh."""
    rep = store.latest_report("pc")
    if not rep:
        return None
    age = time.time() - rep["received"]
    return {**rep, "age": age, "stale": age > PC_STALE_MINUTES * 60}
