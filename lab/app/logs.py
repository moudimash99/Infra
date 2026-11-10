"""Reading the jobs' own logs. Kubernetes deletes a Job (and its log) after a few newer runs, so the lab keeps what
matters from each finished run: a summary per job kind, the failure lines, and the last lines.

Log formats (from the jobs' code):
  bg-taste-match (deploy/run_*.sh, get_audio.py, whisper_captions.py, diarize.py):
    === 2026-11-10T01:17:27Z download hc        a stage starts (download / whisper <ch> / speaker separation / rate)
    [7/41] ok    <id>  <title>  (20 MB)          a download (position in that channel's list)
    [3/41] FAIL  <id>  <title>  (<error>)
    [18/87] <id>  14 min audio in 2.7 min        Whisper
    [5/87] FAILED <id>  <error>
    [2/90] <id>  21 min in 95s  (...)            speaker separation
  BoardGameFinder (scripts/run_pipeline.py):
    2026-11-10 07:27:42 Running step: Scraping prices (src/boardgamefinder/price_finder.py)
    Step classify_postings_with_llm FAILED (Exit Code 1)
"""
import re

BGT_STAGE = re.compile(r"^=== (\S+) (.+)$")
BGT_ITEM = re.compile(r"^\[(\d+)/(\d+)\]\s+(.*)$")
WHISPER = re.compile(r"^(\S+)\s+(\d+) min audio in ([\d.]+) min")
BGF_STEP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) Running step: (.+?) \(([^)]+)\)\s*$")
BGF_FAIL = re.compile(r"^Step (\S+) FAILED \(Exit Code (-?\d+)\)")


def bgt(text):
    """Stages of one pass with their counts, the failure lines, and where it is now."""
    stages, cur = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = BGT_STAGE.match(line)
        if m:
            what = m.group(2)
            if what.startswith(("pass done", "YouTube is refusing")) or "another GPU pass" in what:
                if cur is not None:
                    cur["end_note"] = what
                if "refusing" in what:
                    stages.append({"at": m.group(1), "what": "YouTube refused downloads", "ok": 0, "fail": 0,
                                   "fails": [], "last": None, "pos": None, "audio_min": 0, "took_min": 0.0,
                                   "blocked": True})
                continue
            cur = {"at": m.group(1), "what": what, "ok": 0, "fail": 0, "fails": [], "last": None, "pos": None,
                   "audio_min": 0, "took_min": 0.0, "blocked": False}
            stages.append(cur)
            continue
        if "another GPU pass is running" in line:
            stages.append({"at": line.split(" ")[0], "what": "skipped: another GPU pass was running", "ok": 0,
                           "fail": 0, "fails": [], "last": None, "pos": None, "audio_min": 0, "took_min": 0.0,
                           "blocked": False})
            continue
        m = BGT_ITEM.match(line)
        if m and cur is not None:
            rest = m.group(3)
            cur["pos"], cur["last"] = (int(m.group(1)), int(m.group(2))), rest
            if rest.startswith(("FAIL", "FAILED")):
                cur["fail"] += 1
                cur["fails"].append(line[:300])
            else:
                cur["ok"] += 1
                w = WHISPER.match(rest)
                if w:
                    cur["audio_min"] += int(w.group(2))
                    cur["took_min"] += float(w.group(3))
        elif cur is not None and ("stopping so YouTube" in line or " stopped" in line.lower()):
            cur["fails"].append(line[:300])
    fails = [f for s in stages for f in s["fails"]]
    return {"stages": stages, "now": stages[-1] if stages else None, "fails": fails[-30:],
            "ok": sum(s["ok"] for s in stages), "fail": sum(s["fail"] for s in stages),
            "blocked": any(s["blocked"] for s in stages)}


def bgf(text):
    """BoardGameFinder's steps in order: start time, failed or not, the error lines after a failure."""
    steps, failed = [], {}
    lines = (text or "").splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        m = BGF_STEP.match(line)
        if m:
            script = m.group(3).rsplit("/", 1)[-1].removesuffix(".py")
            steps.append({"at": m.group(1), "label": m.group(2), "key": script, "failed": False, "code": None,
                          "errors": []})
            continue
        m = BGF_FAIL.match(line)
        if m:
            after = [l.strip() for l in lines[i + 1:i + 40] if l.strip() and not l.startswith("=")]
            after = after[:next((k for k, l in enumerate(after) if BGF_STEP.match(l)), len(after))]
            # The error lines first (the step's stdout comes before its stderr in the log).
            errs = [l for l in after if re.search(r"error|fail|unreachable|refused|exception|traceback", l, re.I)
                    and not l.startswith("STDOUT")][:3] or after[:3]
            failed[m.group(1)] = (int(m.group(2)), errs)
    for s in steps:
        if s["key"] in failed:
            s["failed"], (s["code"], s["errors"]) = True, failed[s["key"]]
    return {"steps": steps, "fails": [f"{k}: exit {c}" for k, (c, _) in failed.items()]}


def summarize(kind, text):
    if kind.startswith("bg-taste-match") or kind.startswith("bgt-"):
        return bgt(text)
    if kind in ("boardgamefinder", "bgf"):
        return bgf(text)
    return {}


def tail(text, n=40):
    return "\n".join((text or "").splitlines()[-n:])
