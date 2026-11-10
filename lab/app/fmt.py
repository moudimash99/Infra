"""Formatting used by the templates: relative and local times, durations, compact numbers."""
import datetime as dt
import time

from .config import TZ


def to_ts(value):
    """Epoch seconds from an epoch number, an aware/naive datetime, or an ISO string (naive = UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dt.datetime):
        d = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def local(value):
    ts = to_ts(value)
    return dt.datetime.fromtimestamp(ts, TZ) if ts is not None else None


def ago(value, now=None):
    """'just now', '5 min ago', '3 h ago', 'yesterday 14:05', '2 Nov 14:05'; future: 'in 20 min'."""
    ts = to_ts(value)
    if ts is None:
        return "never"
    now = now or time.time()
    d = now - ts
    if d < 0:
        return "in " + span(-d)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 12 * 3600:
        return f"{int(d // 3600)} h ago"
    when, today = local(ts), dt.datetime.fromtimestamp(now, TZ).date()
    if when.date() == today:
        return f"today {when:%H:%M}"
    if when.date() == today - dt.timedelta(days=1):
        return f"yesterday {when:%H:%M}"
    return f"{when.day} {when:%b %H:%M}"


def clock(value):
    when = local(value)
    if when is None:
        return "-"
    today = dt.datetime.now(TZ).date()
    if when.date() == today:
        return f"{when:%H:%M}"
    if when.date() == today + dt.timedelta(days=1):
        return f"tomorrow {when:%H:%M}"
    return f"{when.day} {when:%b %H:%M}"


def span(seconds):
    """A duration: '45 s', '12 min', '2 h 05', '3 d 4 h'."""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d}"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def num(value, digits=0):
    """1,284 / 12.9K / 4.2M."""
    if value is None:
        return "-"
    v = float(value)
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:.1f}K"
    return f"{v:,.{digits}f}"


def pct(part, whole):
    return 0 if not whole else max(0, min(100, round(100 * part / whole)))


def register(env):
    env.filters.update(ago=ago, clock=clock, span=span, num=num, local=local)
    env.globals.update(pct=pct, now=time.time)
