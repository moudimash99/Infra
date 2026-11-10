"""bg-taste-match: how far each channel is through the pipeline, what's running, what went wrong.

Reads the live working folder read-only (bgtaste.db opened mode=ro, captions/, diar/, the audio folder) and the
Kubernetes jobs. Stages, each counting videos that got at least that far (a transcribed video needs no audio
any more, so the bars only ever grow):
  audio -> text (Whisper captions or a YouTube transcript) -> rated
  audio -> speakers (diarized)
  rated + speakers -> voices (rated again knowing who said what: speaker_transcripts)
"""
import ast
import os
import sqlite3
import time

from . import source, k8s
from .. import logs
from ..config import BGT_AUDIO, BGT_DIR

FETCH_CATEGORIES = ("review", "multi_review", "top_list", "recommendation_list")
CATEGORY_SQL = "COALESCE(category_override, llm_category, category)"
STAGES = [("audio", "Audio"), ("text", "Text"), ("speakers", "Speakers"), ("rated", "Rated"), ("voices", "Voices")]
CRONJOBS = ("bg-taste-match-fetch", "bg-taste-match-gpu", "bg-taste-match-llm")
STAGE_OF_JOB = {"bg-taste-match-fetch": "Downloads", "bg-taste-match-gpu": "Whisper and speakers",
                "bg-taste-match-llm": "Rating"}


def db():
    path = os.path.join(BGT_DIR, "bgtaste.db")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _ids(folder, ext=None):
    """Video ids of the files in a folder (an unfinished download is not a file yet)."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return {}
    out = {}
    for n in names:
        if n.endswith((".part", ".ytdl", ".tmp")) or (ext and not n.endswith(ext)):
            continue
        out[n.rsplit(".", 1)[0]] = os.path.join(folder, n)
    return out


@source("bgt.channels", ttl=3600)
def channel_names():
    """key -> name, from the project's config.py (read as data, not run)."""
    tree = ast.parse(open(os.path.join(BGT_DIR, "config.py"), encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CHANNELS":
            return {c["key"]: c["name"] for c in ast.literal_eval(node.value)}
    return {}


@source("bgt.progress", ttl=120)
def progress():
    conn = db()
    try:
        marks = ",".join("?" * len(FETCH_CATEGORIES))
        # Same scope as the download stage (get_audio.py --any): videos YouTube marked unavailable can't be fetched,
        # so they are counted apart instead of keeping a channel's bars short of 100% forever.
        rows = conn.execute(f"""SELECT id, channel, title, duration, COALESCE(transcript_status, '') AS status
                                FROM videos WHERE {CATEGORY_SQL} IN ({marks})""", FETCH_CATEGORIES).fetchall()
        text = {r[0] for r in conn.execute("SELECT video_id FROM transcripts")}
        rated = {r[0] for r in conn.execute("SELECT video_id FROM ratings UNION SELECT video_id FROM rated_parts")}
        voices = {r[0] for r in conn.execute("SELECT video_id FROM speaker_transcripts")}
    finally:
        conn.close()
    audio = _ids(BGT_AUDIO)
    captions = _ids(os.path.join(BGT_DIR, "captions"), ".json3")
    diar = _ids(os.path.join(BGT_DIR, "diar"), ".json")
    text |= set(captions)
    names = channel_names.get().data or {}
    channels, now = {}, time.time()
    have = text | set(audio) | set(diar)
    unavailable = {}
    videos = []
    for v in rows:
        if v["status"] == "unavailable" and v["id"] not in have:
            unavailable[v["channel"]] = unavailable.get(v["channel"], 0) + 1
        else:
            videos.append(v)
    for v in videos:
        vid, ch = v["id"], v["channel"]
        c = channels.setdefault(ch, {"key": ch, "name": names.get(ch, ch), "videos": 0, "hours": 0.0,
                                     "audio": 0, "text": 0, "speakers": 0, "rated": 0, "voices": 0,
                                     "hours_to_transcribe": 0.0, "to_download": 0})
        c["videos"] += 1
        hours = (v["duration"] or 0) / 3600
        c["hours"] += hours
        # Text and speakers are two separate passes over the audio; rating needs text; voices need both.
        st = {"voices": vid in voices}
        st["rated"] = st["voices"] or vid in rated
        st["text"] = st["rated"] or vid in text
        st["speakers"] = st["voices"] or vid in diar
        st["audio"] = st["text"] or st["speakers"] or vid in audio
        for k, _ in STAGES:
            c[k] += st[k]
        if not st["text"]:
            c["hours_to_transcribe"] += hours
        if not st["audio"]:
            c["to_download"] += 1
            c["hours_to_download"] = c.get("hours_to_download", 0.0) + hours
    for ch, n in unavailable.items():
        if ch in channels:
            channels[ch]["unavailable"] = n
    for c in channels.values():
        c["done"] = c["voices"] == c["videos"]           # nothing left for the pipeline in this channel
        c["started"] = c["audio"] > 0
    # Speed over the last 24 hours, from file times: downloads (audio files) and Whisper (captions).
    day = now - 86400
    dur = {v["id"]: (v["duration"] or 0) for v in videos}
    recent_audio = [i for i, p in audio.items() if _mtime(p) > day]
    recent_caps = [i for i, p in captions.items() if _mtime(p) > day]
    totals = {k: sum(c[k] for c in channels.values()) for k, _ in STAGES}
    totals.update(videos=len(videos), hours=sum(c["hours"] for c in channels.values()),
                  hours_to_transcribe=sum(c["hours_to_transcribe"] for c in channels.values()),
                  hours_to_download=sum(c.get("hours_to_download", 0) for c in channels.values()),
                  to_download=sum(c["to_download"] for c in channels.values()),
                  unavailable=sum(unavailable.values()))
    speed = {"downloads_24h": len(recent_audio),
             "audio_hours_downloaded_24h": sum(dur.get(i, 0) for i in recent_audio) / 3600,
             "transcribed_24h": len(recent_caps),
             "audio_hours_transcribed_24h": sum(dur.get(i, 0) for i in recent_caps) / 3600}
    return {"channels": channels, "totals": totals, "speed": speed, "audio_files": len(audio)}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def stage_label(what, names):
    """'download cotb' -> 'Downloading Chairman of the Board'."""
    words = (what or "").split()
    if not words:
        return what
    ch = names.get(words[-1], words[-1]) if len(words) > 1 else ""
    return {"download": f"Downloading {ch}", "whisper": f"Whisper on {ch}", "rate": f"Rating {ch}"}.get(
        words[0], "Finding speakers" if what.startswith("speaker separation") else what)


def _label_stages(summary, names):
    for s in summary.get("stages", []):
        s["label"] = stage_label(s["what"], names)
    return summary


@source("bgt.now", ttl=20)
def now():
    """Running and recent passes of the three stages (plus passes started by hand), with their log state."""
    jobs = (k8s.jobs.get().data or {"jobs": [], "cronjobs": {}})
    cron = {n: jobs["cronjobs"].get(n) for n in CRONJOBS}
    ours = [j for j in jobs["jobs"] if j["cronjob"] in CRONJOBS or j["name"].startswith("bgt-")]
    names = channel_names.get().data or {}
    out = []
    for j in ours[:12]:
        pods = k8s.pods_of_job(j["name"])
        pod = pods[0] if pods else None
        log = k8s.log_tail.get(j["namespace"], pod["name"], 5000).data if pod else ""
        kind = j["cronjob"] or ("bg-taste-match-gpu" if "gpu" in j["name"] else "bg-taste-match-fetch")
        out.append({**j, "kind": kind, "label": STAGE_OF_JOB.get(kind, kind), "pod": pod, "log": _label_stages(logs.bgt(log), names),
                    "log_tail": logs.tail(log, 15)})
    # YouTube is blocking when the newest finished download pass stopped on it (exit 3, "refusing downloads").
    last_fetch = next((r for r in out if r["kind"] == "bg-taste-match-fetch" and r["state"] != "running"), None)
    blocked = bool(last_fetch and last_fetch["log"]["blocked"])
    return {"runs": out, "cron": cron, "youtube_blocked": blocked,
            "gpu_paused": bool(cron.get("bg-taste-match-gpu") and cron["bg-taste-match-gpu"]["suspended"])}


@source("bgt.activity", ttl=120)
def activity(limit=40):
    """Newest events across stages: downloads and Whisper (file times), ratings (rated_at)."""
    audio = _ids(BGT_AUDIO)
    caps = _ids(os.path.join(BGT_DIR, "captions"), ".json3")
    diar = _ids(os.path.join(BGT_DIR, "diar"), ".json")
    ev = [(_mtime(p), "Downloaded", i) for i, p in audio.items()]
    ev += [(_mtime(p), "Transcribed", i) for i, p in caps.items()]
    ev += [(_mtime(p), "Speakers found", i) for i, p in diar.items()]
    ev = sorted(ev, reverse=True)[:limit]
    conn = db()
    try:
        rated = conn.execute("""SELECT video_id, MAX(rated_at) AS at, COUNT(*) AS n FROM ratings
                                GROUP BY video_id ORDER BY at DESC LIMIT ?""", (limit,)).fetchall()
        from ..fmt import to_ts
        ev += [(to_ts(r["at"]) or 0, f"Rated ({r['n']} verdicts)", r["video_id"]) for r in rated]
        ev = sorted(ev, reverse=True)[:limit]
        ids = list({e[2] for e in ev})
        info = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT id, channel, title, duration FROM videos WHERE id IN ({','.join('?' * len(ids))})", ids)}
    finally:
        conn.close()
    return [{"at": t, "what": w, "id": i, **info.get(i, {"title": i, "channel": "", "duration": None})} for t, w, i in ev]


@source("bgt.problems", ttl=300)
def problems():
    conn = db()
    try:
        unmatched = [dict(r) for r in conn.execute("""SELECT g.name, g.method, COUNT(r.video_id) AS ratings
                       FROM games g LEFT JOIN ratings r ON r.game = g.name WHERE g.bgg_id IS NULL
                       GROUP BY g.name ORDER BY ratings DESC, g.name LIMIT 30""")]
        n_unmatched = conn.execute("SELECT COUNT(*) FROM games WHERE bgg_id IS NULL").fetchone()[0]
        # Text ready but not rated yet: waiting for the rating stage (fm-llm).
        marks = ",".join("?" * len(FETCH_CATEGORIES))
        waiting = conn.execute(f"""SELECT COUNT(*) FROM videos v JOIN transcripts t ON t.video_id = v.id
                   WHERE {CATEGORY_SQL} IN ({marks}) AND v.id NOT IN (SELECT video_id FROM ratings)
                   AND v.id NOT IN (SELECT video_id FROM rated_parts)""", FETCH_CATEGORIES).fetchone()[0]
    finally:
        conn.close()
    return {"unmatched": unmatched, "n_unmatched": n_unmatched, "waiting_rating": waiting}


@source("bgt.video", ttl=60)
def video(vid):
    conn = db()
    try:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        if not v:
            return None
        ratings = [dict(r) for r in conn.execute("""SELECT r.game, r.score, r.confidence, r.identified_by, r.quote,
                       r.rated_at, r.model, COALESCE(rv.name, r.reviewer_id) AS reviewer, g.bgg_id
                       FROM ratings r LEFT JOIN reviewers rv ON rv.id = r.reviewer_id
                       LEFT JOIN games g ON g.name = r.game WHERE r.video_id = ? ORDER BY r.game""", (vid,))]
        t = conn.execute("SELECT lang, text FROM transcripts WHERE video_id = ?", (vid,)).fetchone()
        sp = conn.execute("SELECT speakers FROM speaker_transcripts WHERE video_id = ?", (vid,)).fetchone()
        errors = [dict(r) for r in conn.execute("SELECT error, at FROM fetch_errors WHERE video_id = ? ORDER BY at DESC",
                                                (vid,))]
    finally:
        conn.close()
    has = lambda folder, ext: os.path.exists(os.path.join(folder, vid + ext))
    audio = any(i == vid for i in _ids(BGT_AUDIO))
    return {"video": dict(v), "ratings": ratings, "transcript": dict(t) if t else None,
            "speakers": sp["speakers"] if sp else None, "errors": errors,
            "flags": {"audio": audio, "captions": has(os.path.join(BGT_DIR, "captions"), ".json3"),
                      "diar": has(os.path.join(BGT_DIR, "diar"), ".json"), "voices": bool(sp)}}


@source("bgt.search", ttl=60)
def search(q):
    q = q.strip()
    if len(q) < 2:
        return []
    conn = db()
    try:
        like = f"%{q}%"
        rows = conn.execute(f"""SELECT v.id, v.channel, v.title, v.duration, v.timestamp, {CATEGORY_SQL} AS category,
                   (SELECT COUNT(*) FROM ratings r WHERE r.video_id = v.id) AS n_ratings
                   FROM videos v WHERE v.title LIKE ? OR v.id IN (SELECT video_id FROM ratings WHERE game LIKE ?)
                   ORDER BY v.timestamp DESC LIMIT 60""", (like, like)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def results_file():
    p = os.path.join(BGT_DIR, "results.html")
    return p if os.path.exists(p) else None
