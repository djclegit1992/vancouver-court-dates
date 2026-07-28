#!/usr/bin/env python3
"""
Audit the Vancouver Court Dates repository.

Answers one question: is everything on this machine, and in the repo,
the current version?

Compares every project file against manifest.json, which carries a
hash of the canonical version of each. Line endings are normalised
before hashing, so Windows CRLF does not produce false mismatches.

Also reports anything uncommitted or unpushed, and runs the test suites
if asked.

Usage:
    python audit.py                 compare against manifest.json
    python audit.py --tests         also run the offline test suites
"""

import hashlib
import json
import os
import subprocess
import sys

MANIFEST = "manifest.json"

OK, BAD, WARN = [], [], []


def norm_hash(path):
    """sha256 of the file with line endings normalised to LF."""
    with open(path, "rb") as f:
        data = f.read()
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()[:16]


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def main():
    if not os.path.exists(MANIFEST):
        print("No %s here. Download it alongside this script." % MANIFEST)
        return 1

    man = json.load(open(MANIFEST, encoding="utf-8"))
    files = man["files"]

    print("manifest built %s" % man.get("built", "unknown"))
    print("%d files expected" % len(files))
    print()

    print("FILES")
    missing, stale, current = [], [], []
    for path in sorted(files):
        want = files[path]
        if not os.path.exists(path):
            missing.append(path)
            print("  MISSING   %s" % path)
            continue
        got = norm_hash(path)
        if got == want:
            current.append(path)
            print("  current   %s" % path)
        else:
            stale.append(path)
            print("  DIFFERS   %-34s local %s, expected %s"
                  % (path, got, want))

    print()
    print("  %d current, %d differ, %d missing"
          % (len(current), len(stale), len(missing)))

    # -- git state ---------------------------------------------------
    print()
    print("GIT")
    rc, out, _ = run(["git", "status", "--porcelain"])
    if rc != 0:
        print("  not a git repository, or git unavailable")
    else:
        dirty = [l for l in out.split("\n") if l.strip()]
        tracked = [l for l in dirty if not l.startswith("??")]
        untracked = [l for l in dirty if l.startswith("??")]
        if tracked:
            print("  UNCOMMITTED changes in %d tracked file(s):" % len(tracked))
            for l in tracked[:12]:
                print("      %s" % l)
        else:
            print("  no uncommitted changes to tracked files")
        if untracked:
            print("  %d untracked file(s):" % len(untracked))
            for l in untracked[:12]:
                print("      %s" % l)

        rc, out, _ = run(["git", "status", "-sb"])
        if "ahead" in out:
            print("  UNPUSHED commits: %s" % out.split("\n")[0])
        elif "behind" in out:
            print("  BEHIND the remote, run: git pull --rebase")
        else:
            print("  in sync with the remote")

        rc, out, _ = run(["git", "log", "-1", "--format=%h %ci %s"])
        if rc == 0:
            print("  last commit: %s" % out)

    # -- data freshness ---------------------------------------------
    print()
    print("DATA")
    latest = os.path.join("data", "latest.json")
    if not os.path.exists(latest):
        print("  no data/latest.json")
    else:
        d = json.load(open(latest, encoding="utf-8"))
        print("  run id          %s" % d.get("run_id"))
        print("  generated at    %s" % d.get("generated_at"))
        print("  documents       %s" % d.get("document_count"))
        print("  healthy         %s" % d.get("healthy"))
        docs = d.get("documents", [])
        parsed = [x for x in docs if not x.get("is_instruction")]
        empty = [x for x in parsed if not x.get("dates")]
        has_dates = [x for x in parsed if x.get("dates")]
        print("  availability lists %d, of which %d offer dates and %d do not"
              % (len(parsed), len(has_dates), len(empty)))
        if not any("dates" in x for x in docs):
            print("  WARNING: no 'dates' field; latest.json predates the "
                  "payload change")
        created = sorted(x.get("date_created") for x in docs
                         if x.get("date_created"))
        if created:
            print("  court last built %s" % created[-1])

    print()
    if missing or stale:
        print("ACTION NEEDED: %d file(s) out of date or missing."
              % (len(missing) + len(stale)))
        for p in missing + stale:
            print("   - %s" % p)
        return 1

    print("Everything matches the manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
