#!/usr/bin/env python3
"""A "Where it runs" section in the README of every repo that services.yaml names, made from its entries.

So someone reading a project's README sees where it runs, its address, how it is deployed, its manifest and data,
without opening Infra. services.yaml stays the one place to edit: the section sits between the markers below and is
rewritten whole each time (added at the end of the README the first time; the rest of the README is never touched).

  python scripts/readmes.py           write the sections (then commit each repo's README.md with its other changes)
  python scripts/readmes.py --check   only list the READMEs whose section is missing or out of date (exit 1 if any)

A repo is found from an entry's code.repo and code.libs (a path, or a glob like fm\\fm-*, before any " ("); a path
inside a repo counts for that repo. Infra itself is skipped: its own README is the deployment guide.
The section only repeats what is in services.yaml, which is public, so it is safe for public repos too.
"""
import glob
import os
import subprocess
import sys

import yaml

INFRA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = "<!-- where-it-runs: made by Infra/scripts/readmes.py from Infra/services.yaml; edit the entry there, not here -->"
END = "<!-- /where-it-runs -->"
CATALOG = "https://github.com/moudimash99/Infra/blob/main/services.yaml"
ROWS = [("Runs", "runs"), ("Address", "url"), ("Schedule", "schedule"), ("Deploy", "deploy"), ("Manifest", "manifest"),
        ("Image", "image"), ("Data", "data"), ("Check", "check"), ("Depends on", "depends_on")]


def toplevel(path):
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return os.path.abspath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def paths(entry):
    pat = str(entry or "").split(" (")[0].strip()
    if not pat or not os.path.isabs(pat):
        return []
    return [p for p in (glob.glob(pat) if "*" in pat else [pat]) if os.path.isdir(p)]


def repos_of(services):
    """{repo toplevel: [(entry, "own" or "lib")]}, in services.yaml order."""
    out, infra = {}, os.path.normcase(os.path.abspath(INFRA))
    seen = {}   # normcase(path) -> the path as git prints it, so Windows' case differences make one repo
    for svc in services:
        code = svc.get("code") if isinstance(svc.get("code"), dict) else {}
        found = [(p, "own") for p in paths(code.get("repo"))] + [(p, "lib") for lib in code.get("libs") or [] for p in paths(lib)]
        for p, role in found:
            top = toplevel(p)
            if not top or os.path.normcase(top) == infra:
                continue
            top = seen.setdefault(os.path.normcase(top), top)
            if (svc, role) not in out.setdefault(top, []):
                out[top].append((svc, role))
    return out


def cell(v):
    if v is None or v == [] or v == "":
        return None
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v)
    return " ".join(str(v).split()).replace("|", "\\|")


def section(entries):
    lines = [START, "## Where it runs", ""]
    own = [svc for svc, role in entries if role == "own"]
    libs = [svc for svc, role in entries if role == "lib"]
    for svc in own:
        if len(own) > 1:
            lines += [f"### {svc['name']}", ""]
        lines += [f"**{svc['name']}** ({svc.get('kind')}, {svc.get('lifecycle')}): {cell(svc.get('what'))}", "",
                  "| | |", "|---|---|"]
        lines += [f"| {label} | {cell(svc.get(key))} |" for label, key in ROWS if cell(svc.get(key))]
        drift = [d for d in svc.get("drift") or [] if d]
        if drift:
            lines += ["", "Where what runs differs from the code:", ""] + [f"- {cell(d)}" for d in drift]
        lines.append("")
    if libs:
        lines += ["Built into: " + ", ".join(f"**{svc['name']}** ({cell(svc.get('runs'))})" for svc in libs)
                  + ". Changing this repo changes them at their next build.", ""]
    names = ", ".join(f"`{svc['name']}`" for svc, _ in entries)
    lines += [f"From the home's service catalog, [Infra/services.yaml]({CATALOG}) (entries {names}). What runs right now,",
              "its health, and code here that is not deployed yet: `python scripts/status.py` in the Infra repo.", END]
    return lines


def updated(text, block, eol):
    body = eol.join(block)
    if START in text and END in text:
        head, rest = text.split(START, 1)
        return head + body + rest.split(END, 1)[1]
    return text.rstrip("\r\n") + eol * 2 + body + eol


def main():
    check = "--check" in sys.argv
    services = yaml.safe_load(open(os.path.join(INFRA, "services.yaml"), encoding="utf-8"))
    stale = []
    for repo, entries in sorted(repos_of(services).items()):
        readme = os.path.join(repo, "README.md")
        text = open(readme, encoding="utf-8", newline="").read() if os.path.exists(readme) else f"# {os.path.basename(repo)}\n"
        eol = "\r\n" if "\r\n" in text else "\n"
        new = updated(text, section(entries), eol)
        if new != text:
            stale.append(repo)
            if not check:
                with open(readme, "w", encoding="utf-8", newline="") as f:
                    f.write(new)
        print(f"{'out of date' if new != text and check else 'written' if new != text else 'up to date'}: "
              f"{readme} ({', '.join(svc['name'] for svc, _ in entries)})")
    if check and stale:
        sys.exit(1)


if __name__ == "__main__":
    main()
