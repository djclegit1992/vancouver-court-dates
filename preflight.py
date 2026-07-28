#!/usr/bin/env python3
"""
Preflight for the Vancouver watcher.

Three requests. Writes nothing. Run this before scrape.py ever touches
the site, because if something is wrong you want to find out on one
document rather than thirty-one.

  1. Load the index and run collect_links against real markup
  2. Fetch one document cold, hash it, extract its text
  3. Fetch the same document again with the stored ETag, expect 304

Usage:
    python preflight.py
    python preflight.py "3 Day Civil Trials"
"""

import sys
import time

import scrape

DEFAULT_DOC = "16 Day & Over MVA Trials"

OK, BAD = [], []


def ok(msg):
    OK.append(msg)
    print("  PASS  %s" % msg)


def bad(msg):
    BAD.append(msg)
    print("  FAIL  %s" % msg)


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOC
    session = scrape.make_session()

    print("User-Agent: %s" % scrape.USER_AGENT)
    print("trust store: %s" % scrape.TRUST_STORE)
    print()

    # -- 1. index ---------------------------------------------------
    print("1. index page")
    try:
        r = scrape.fetch(session, scrape.INDEX_URL)
    except Exception as e:
        bad("index unreachable: %s" % e)
        return 1

    print("     HTTP %d, %d bytes" % (r.status_code, len(r.content)))
    if r.status_code != 200:
        bad("expected 200")
        return 1
    ok("index loaded")

    docs = scrape.collect_links(r.text, r.url)
    print("     collect_links found %d documents" % len(docs))
    if len(docs) == 31:
        ok("31 documents, matches the validated count")
    elif len(docs) >= scrape.MIN_DOCUMENTS:
        bad("found %d, expected 31. Page may have changed; check the list"
            % len(docs))
    else:
        bad("found %d, below the floor of %d. Stop and investigate."
            % (len(docs), scrape.MIN_DOCUMENTS))
        return 1

    match = [d for d in docs if d["title"] == want]
    if not match:
        bad("no document titled %r. Available titles:" % want)
        for d in docs:
            print("       %s" % d["title"])
        return 1
    doc = match[0]
    print("     testing: %s" % doc["title"])
    print("     url    : %s" % doc["url"])

    # -- 2. cold fetch ----------------------------------------------
    print()
    print("2. cold fetch")
    time.sleep(scrape.REQUEST_DELAY)
    t0 = time.time()
    rec = scrape.check_document(session, doc, None)
    elapsed = time.time() - t0

    print("     status      %s" % rec["status"])
    print("     http        %s" % rec["http_status"])
    print("     size        %s bytes" % rec["size_bytes"])
    print("     elapsed     %.1fs" % elapsed)
    print("     ETag        %s" % rec["etag"])
    print("     Last-Mod    %s" % rec["last_modified"])
    print("     bytes_hash  %s" % (rec["bytes_hash"] or "")[:32])
    print("     text_hash   %s" % (rec["text_hash"] or "")[:32])
    print("     readable    %s" % rec["readable"])
    if rec["note"]:
        print("     note        %s" % rec["note"])

    if rec["status"] == "FETCH_FAILED":
        bad("could not fetch: %s" % rec["note"])
        return 1
    ok("document fetched")

    if rec["status"] != "FIRST_SEEN":
        bad("expected FIRST_SEEN on a cold fetch, got %s" % rec["status"])
    else:
        ok("cold fetch reports FIRST_SEEN")

    if rec["readable"]:
        ok("text extracted, content-level change detection available")
    else:
        bad("NO EXTRACTABLE TEXT. Not fatal, but this document can only be "
            "watched by bytes, so every re-export will alert. Tier two "
            "parsing will not work on it either.")

    if not rec["etag"] and not rec["last_modified"]:
        bad("no ETag and no Last-Modified. Every run downloads every file.")
    else:
        ok("validator present, conditional requests will work")

    # -- 3. conditional refetch -------------------------------------
    print()
    print("3. conditional refetch, expecting 304")
    time.sleep(scrape.REQUEST_DELAY)
    t0 = time.time()
    rec2 = scrape.check_document(session, doc, rec)
    elapsed = time.time() - t0

    print("     status      %s" % rec2["status"])
    print("     http        %s" % rec2["http_status"])
    print("     elapsed     %.1fs" % elapsed)

    if rec2["http_status"] == 304:
        ok("server honoured the conditional request, no body transferred")
    elif rec2["status"] == "UNCHANGED":
        ok("unchanged, but the server re-sent the body. Runs will be "
           "heavier than planned.")
    else:
        bad("expected UNCHANGED, got %s. The document changed between two "
            "requests seconds apart, which is unlikely, or comparison is "
            "broken." % rec2["status"])

    # -- summary ----------------------------------------------------
    print()
    print("%d passed, %d failed" % (len(OK), len(BAD)))
    if BAD:
        print()
        for b in BAD:
            print("  %s" % b)
        print()
        print("Do not run scrape.py until these are understood.")
        return 1

    print()
    print("Clear. A full cold run will make 32 requests over about 62s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
