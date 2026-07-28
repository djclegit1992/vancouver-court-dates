#!/usr/bin/env python3
"""
Offline self-test for the Vancouver watcher.

Builds real PDFs with a minimal built-in writer, serves them through a
fake session, and walks the watcher through every state transition it
can produce. No network. The point is to prove the diff logic before it ever touches
bccourts.ca.

The transition that matters most is REEXPORTED. A PDF re-saved with no
substantive edit gets a fresh /CreationDate and therefore fresh bytes.
If that fires an alert, subscribers learn to ignore the feed.
"""

import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape
import testpdf


def make_pdf(lines, creation_date=None):
    """A real PDF. Passing a different creation_date changes the bytes
    while leaving the extracted text identical, which is how a re-export
    with no substantive edit is simulated."""
    return testpdf.make_pdf(lines, producer=creation_date)


INDEX_HTML = """
<html><body>
<div id="abbotsford">
  <a href="/supreme_court/scheduling/lists/Abbotsford/CPC%20dates%20-%20AB.pdf">CPC dates</a>
  <a href="/supreme_court/scheduling/lists/Abbotsford/JCC%20dates%20-%20AB.pdf">JCC dates</a>
</div>
<div id="vancouver">
  <a href="/supreme_court/scheduling/lists/Vancouver/2%20Day%20Civil%20Trials.pdf">2 Day Civil Trials</a>
  <a href="/Supreme_Court/scheduling/lists/vancouver/3%20Day%20Civil%20Trials.pdf">3 Day Civil</a>
  <a href="lists/Vancouver/16%20Day%20&amp;%20Over%20MVA%20Trials.pdf">16 Day &amp; Over MVA</a>
  <a href="/supreme_court/scheduling/lists/Vancouver/Booking%20Trials.pdf">Booking Trials</a>
  <a href="/supreme_court/scheduling/lists/Vancouver/2%20Day%20Civil%20Trials.pdf">duplicate link</a>
  <a href="#top">back to top</a>
  <a href="mailto:sc.civil_va@bccourts.ca">email</a>
</div>
<div id="victoria">
  <a href="/supreme_court/scheduling/lists/Victoria/Booking%20Trials.pdf">Booking Trials</a>
</div>
</body></html>
"""


class FakeResponse(object):
    def __init__(self, status, content=b"", headers=None, url=""):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.url = url
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise scrape.requests.HTTPError("%d" % self.status_code)


class FakeSession(object):
    """Serves a scripted world. Honours If-None-Match like IIS does."""

    def __init__(self):
        self.files = {}      # url -> (content, etag, last_modified)
        self.fail = set()    # urls that raise
        self.bad_type = set()
        self.calls = 0

    def put(self, url, content, etag, lm="Mon, 27 Jul 2026 17:03:45 GMT"):
        self.files[url] = (content, etag, lm)

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls += 1
        headers = headers or {}

        if scrape.INDEX_URL in url:
            return FakeResponse(200, INDEX_HTML.encode("utf-8"),
                                {"Content-Type": "text/html"}, url)

        if url in self.fail:
            raise scrape.requests.ConnectionError("simulated")

        if url not in self.files:
            return FakeResponse(404, b"", {}, url)

        content, etag, lm = self.files[url]

        if headers.get("If-None-Match") == etag:
            return FakeResponse(304, b"", {"ETag": etag,
                                           "Last-Modified": lm}, url)

        ctype = "text/html" if url in self.bad_type else "application/pdf"
        return FakeResponse(200, content, {
            "Content-Type": ctype, "ETag": etag, "Last-Modified": lm,
            "Content-Length": str(len(content)),
        }, url)


PASS, FAIL = [], []


def check(label, got, want):
    if got == want:
        PASS.append(label)
        print("  ok    %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL  %s: got %r, want %r" % (label, got, want))


def statuses():
    """Per-document status from state.json, which is written on every run.
    latest.json is deliberately withheld when a run is quarantined, so it
    is the wrong file to assert run outcomes against."""
    d = scrape.load_json("data/state.json", {})
    return {e["title"]: e["status"] for e in d.values()}


def published():
    """What the public page would actually see."""
    d = scrape.load_json("data/latest.json", {})
    return {x["title"]: x for x in d.get("documents", [])}


def main():
    tmp = tempfile.mkdtemp(prefix="vanwatch-")
    os.chdir(tmp)
    print("workspace %s\n" % tmp)

    sess = FakeSession()
    scrape.make_session = lambda: sess
    scrape.REQUEST_DELAY = 0
    scrape.MIN_DOCUMENTS = 3

    V = "https://www.bccourts.ca/supreme_court/scheduling/lists/Vancouver/"
    U2 = V + "2%20Day%20Civil%20Trials.pdf"
    U3 = ("https://www.bccourts.ca/Supreme_Court/scheduling/lists/vancouver/"
          "3%20Day%20Civil%20Trials.pdf")
    U16 = V + "16%20Day%20&%20Over%20MVA%20Trials.pdf"
    UB = V + "Booking%20Trials.pdf"

    body_a = ["Available 2 day civil trial dates", "September 14 2026",
              "October 5 2026"]
    body_b = ["Available 2 day civil trial dates", "October 5 2026",
              "November 16 2026"]

    sess.put(U2, make_pdf(body_a), '"aaa:0"')
    sess.put(U3, make_pdf(["3 day civil", "January 12 2027"]), '"bbb:0"')
    sess.put(U16, make_pdf(["16 day MVA", "March 1 2028"]), '"ccc:0"')
    sess.put(UB, make_pdf(["How to book a trial"]), '"ddd:0"')

    # ---------------------------------------------------------------
    print("link collection")
    docs = scrape.collect_links(INDEX_HTML, scrape.INDEX_URL)
    titles = sorted(d["title"] for d in docs)
    check("only Vancouver, deduplicated", len(docs), 4)
    check("titles decoded from filenames", titles,
          ["16 Day & Over MVA Trials", "2 Day Civil Trials",
           "3 Day Civil Trials", "Booking Trials"])
    check("mixed-case paths both matched",
          any("/Supreme_Court/" in d["url"] for d in docs), True)
    check("relative href resolved",
          any(d["url"].startswith(
              "https://www.bccourts.ca/supreme_court/scheduling/lists/"
              "Vancouver/16") for d in docs), True)
    check("instruction doc flagged",
          [d["is_instruction"] for d in docs
           if d["title"] == "Booking Trials"], [True])
    check("availability list not flagged",
          [d["is_instruction"] for d in docs
           if d["title"] == "2 Day Civil Trials"], [False])

    # ---------------------------------------------------------------
    print("\nrun 1, cold start")
    rc = scrape.main()
    s = statuses()
    check("exit 0", rc, 0)
    check("all first seen",
          sorted(set(s.values())), ["FIRST_SEEN"])
    check("four documents", len(s), 4)

    # ---------------------------------------------------------------
    print("\nrun 2, nothing changed, conditional requests honoured")
    os.environ["FORCE_RUN"] = "1"
    before = sess.calls
    rc = scrape.main()
    s = statuses()
    check("exit 0", rc, 0)
    check("all unchanged", sorted(set(s.values())), ["UNCHANGED"])
    check("one index call plus four conditional", sess.calls - before, 5)

    # ---------------------------------------------------------------
    print("\nrun 3, one genuine content change")
    sess.put(U2, make_pdf(body_b), '"aaa2:0"')
    rc = scrape.main()
    s = statuses()
    check("exit 0", rc, 0)
    check("changed doc is UPDATED", s["2 Day Civil Trials"], "UPDATED")
    check("others untouched", s["3 Day Civil Trials"], "UNCHANGED")

    # ---------------------------------------------------------------
    print("\nrun 4, re-export with identical text")
    sess.put(U2, make_pdf(body_b, creation_date="different-producer"),
             '"aaa3:0"')
    rc = scrape.main()
    s = statuses()
    check("exit 0", rc, 0)
    check("re-export not reported as UPDATED",
          s["2 Day Civil Trials"], "REEXPORTED")

    # ---------------------------------------------------------------
    print("\nrun 5, unreadable file")
    sess.put(U3, b"%PDF-1.4 this is not a real pdf", '"bbb2:0"')
    rc = scrape.main()
    s = statuses()
    check("exit 0", rc, 0)
    check("unreadable file still reported as a change",
          s["3 Day Civil Trials"], "UPDATED")
    st = scrape.load_json("data/state.json", {})
    entry = [e for e in st.values() if e["title"] == "3 Day Civil Trials"][0]
    check("marked not readable", entry["readable"], False)
    check("stale text hash discarded", entry["text_hash"], None)

    # An unreadable file that does not change must go quiet, not shout
    # UNREADABLE every run and eventually quarantine the job.
    rc = scrape.main()
    s2 = statuses()
    check("unchanged unreadable file goes quiet",
          s2["3 Day Civil Trials"], "UNCHANGED")
    st2 = scrape.load_json("data/state.json", {})
    e2 = [e for e in st2.values() if e["title"] == "3 Day Civil Trials"][0]
    check("still flagged unreadable while quiet", e2["readable"], False)

    # A scan-like PDF: valid file, zero extractable text.
    print("\nrun 5b, image-only pdf with no extractable text")
    blank = make_pdf([])
    sess.put(U3, blank, '"bbb-blank:0"')
    scrape.main()
    st3 = scrape.load_json("data/state.json", {})
    e3 = [e for e in st3.values() if e["title"] == "3 Day Civil Trials"][0]
    check("empty extraction is not readable", e3["readable"], False)
    check("empty extraction stores no text hash", e3["text_hash"], None)

    # ---------------------------------------------------------------
    print("\nrun 6, wrong content type served with 200")
    sess.put(U3, make_pdf(["3 day civil", "January 12 2027"]), '"bbb3:0"')
    sess.bad_type.add(U3)
    rc = scrape.main()
    s = statuses()
    check("html body rejected, not published as data",
          s["3 Day Civil Trials"], "FETCH_FAILED")
    sess.bad_type.discard(U3)

    # ---------------------------------------------------------------
    print("\nrun 7, transport failure streak")
    sess.fail.add(U16)
    codes = []
    for i in range(3):
        sess.put(U16, make_pdf(["16 day MVA", "March 1 2028"]),
                 '"ccc%d:0"' % i)
        codes.append(scrape.main())
    check("third consecutive failure fails the job", codes[-1], 1)
    sess.fail.discard(U16)

    # ---------------------------------------------------------------
    print("\nrun 8, quarantine on mass failure")
    good_run = scrape.load_json("data/latest.json", {})["run_id"]
    for u in (U2, U3, U16, UB):
        sess.fail.add(u)
    rc = scrape.main()
    check("exit non-zero", rc, 1)
    check("latest.json untouched",
          scrape.load_json("data/latest.json", {})["run_id"], good_run)
    check("quarantined file written",
          os.path.exists("data/quarantined.json"), True)
    for u in (U2, U3, U16, UB):
        sess.fail.discard(u)

    # ---------------------------------------------------------------
    print("\nrun 9, index shape collapses")
    real = scrape.collect_links
    scrape.collect_links = lambda h, u: real(h, u)[:1]
    rc = scrape.main()
    check("below floor, refuses to publish", rc, 1)
    scrape.collect_links = real

    # ---------------------------------------------------------------
    print("\nskip guard")
    del os.environ["FORCE_RUN"]
    before = sess.calls
    scrape.main()
    check("no requests made", sess.calls - before, 0)
    os.environ["FORCE_RUN"] = "1"

    # ---------------------------------------------------------------
    print("\narchive and history")
    n = sum(len(files) for _, _, files in os.walk("data/raw"))
    check("every distinct version archived once", n >= 6, True)
    with open("data/history.csv") as f:
        rows = len(f.readlines())
    check("history has a header plus rows", rows > 20, True)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        for f_ in FAIL:
            print("  FAILED: %s" % f_)
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
