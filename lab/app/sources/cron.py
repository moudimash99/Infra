"""CronJob schedules: when a job should have run (to compare with when it did) and the schedule in plain words.

Kubernetes reads a schedule in the CronJob's time zone (kube_cronjob_info's timezone label; "local" is the machine's,
Europe/Paris here). Five fields: minute hour day-of-month month day-of-week, each "*", "*/n", "a", "a-b", "a-b/n" or a
list of those. Times are shown in the lab's time zone.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from ..config import TZ

RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def field(text, lo, hi):
    out = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
            if step > 1:
                b = hi
        out.update(range(a, b + 1, step))
    return out


def parse(schedule):
    parts = schedule.split()
    if len(parts) != 5:
        return None
    try:
        sets = [field(p, lo, hi) for p, (lo, hi) in zip(parts, RANGES)]
    except ValueError:
        return None
    sets[4] = {d % 7 for d in sets[4]}            # 7 is Sunday too
    return sets, parts


def zone(tz):
    if not tz or tz == "local":
        return TZ
    try:
        return ZoneInfo(tz)
    except Exception:                               # noqa: BLE001 - an unknown zone: the lab's own
        return TZ


def times(schedule, tz, start, end):
    """Every time the schedule fires in [start, end], as epoch seconds."""
    p = parse(schedule)
    if not p:
        return []
    (mins, hours, doms, months, dows), parts = p
    z = zone(tz)
    day = dt.datetime.fromtimestamp(start, z).replace(hour=0, minute=0, second=0, microsecond=0)
    last = dt.datetime.fromtimestamp(end, z)
    out = []
    while day <= last:
        dow = (day.weekday() + 1) % 7               # cron counts from Sunday
        dom_any, dow_any = parts[2] == "*", parts[4] == "*"
        dom_ok, dow_ok = day.day in doms, dow in dows
        ok = (dom_ok and dow_ok) if (dom_any or dow_any) else (dom_ok or dow_ok)
        if ok and day.month in months:
            for h in sorted(hours):
                for m in sorted(mins):
                    t = day.replace(hour=h, minute=m).timestamp()
                    if start <= t <= end:
                        out.append(t)
        day = (day + dt.timedelta(days=1, hours=2)).replace(hour=0)       # past a daylight-saving change
    return out


def words(schedule, tz):
    """'*/3 * * * *' -> 'every 3 min'; '20 0,19 * * *' -> 'daily at 00:20 and 19:20' (in the lab's time)."""
    p = parse(schedule)
    if not p:
        return schedule
    (mins, hours, doms, months, dows), parts = p
    if parts[2] != "*" and parts[3] != "*" and len(doms) == 1 and len(months) == 1:
        return "once a year: it is started by hand"
    local = lambda h, m: (dt.datetime.now(zone(tz)).replace(hour=h, minute=m, second=0, microsecond=0)
                          .astimezone(TZ).strftime("%H:%M"))
    if parts[1] == "*" and parts[0].startswith("*/"):
        return f"every {parts[0][2:]} min"
    if parts[1] == "*" and len(mins) == 1:
        return f"every hour at :{min(mins):02d}"
    m = min(mins) if len(mins) == 1 else None
    if m is not None and parts[1].startswith("*/"):
        return f"every {parts[1][2:]} hours at :{m:02d}"
    if m is not None and len(hours) > 3:
        runs = []
        hs = sorted(hours)
        a = b = hs[0]
        for h in hs[1:]:
            if h == b + 1:
                b = h
            else:
                runs.append((a, b))
                a = b = h
        runs.append((a, b))
        spans = " and ".join(f"{local(x, m)}–{local(y, m)}" for x, y in runs)
        return f"every hour at :{m:02d}, {spans}"
    if m is not None:
        at = " and ".join(local(h, m) for h in sorted(hours))
        if parts[4] != "*" and len(dows) == 1:
            return f"every {DAYS[min(dows)]} at {at}"
        return f"daily at {at}"
    return schedule
