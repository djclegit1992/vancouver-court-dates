#!/usr/bin/env python3
"""
End-to-end integration test.

Serves the 31 real Vancouver PDFs through a fake session at their real
URLs and runs the full pipeline. The question this answers, which
neither unit suite can: does the grid survive a run where every
document returns 304 and no bytes are downloaded?

That is the normal case. Four runs out of five will be all-304, and if
the grid only builds when files are fetched, the page goes blank most
of the day.

Usage:  python test_integration.py <dir-of-real-pdfs>
"""

import os
import re
import shutil
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape
from testpdf import make_bcsc_pdf

PASS, FAIL = [], []
AS_AT = date(2026, 7, 28)

BASE = ("https://www.bccourts.ca/supreme_court/scheduling/lists/"
        "Vancouver/")

# slug -> the filename the court actually uses
FILENAMES = {
    "2-day-civil-trials": "2 Day Civil Trials.pdf",
    "3-day-civil-trials": "3 Day Civil Trials.pdf",
    "4-5-day-civil-trials": "4-5 Day Civil Trials.pdf",
    "6-15-day-civil-trials": "6-15 Day Civil Trials.pdf",
    "16-day-over-civil-trials": "16 Day & Over Civil Trials.pdf",
    "2-day-family-trials": "2 Day Family Trials.pdf",
    "3-day-family-trials": "3 Day Family Trials.pdf",
    "4-5-day-family-trials": "4-5 Day Family Trials.pdf",
    "6-15-day-family-trials": "6-15 Day Family Trials.pdf",
    "16-day-over-family-trials": "16 Day & Over Family Trials.pdf",
    "2-day-mva-trials": "2 Day MVA Trials.pdf",
    "3-day-mva-trials": "3 Day MVA Trials.pdf",
    "4-5-day-mva-trials": "4-5 Day MVA Trials.pdf",
    "6-15-day-mva-trials": "6-15 Day MVA Trials.pdf",
    "16-day-over-mva-trials": "16 Day & Over MVA Trials.pdf",
    "case-planning-conference-available-dates":
        "Case Planning Conference Available Dates.pdf",
    "judicial-case-conference-available-dates":
        "Judicial Case Conference Available Dates.pdf",
    "pre-trial-conference-available-dates":
        "Pre-Trial Conference Available Dates.pdf",
    "settlement-conference-available-dates":
        "Settlement Conference Available Dates.pdf",
    "trial-management-conference-available-dates":
        "Trial Management Conference Available Dates.pdf",
    "sca-available-dates": "SCA Available Dates.pdf",
    "assize-chambers-dates": "Assize Chambers Dates.pdf",
    "civil-lengthy-chambers-available-dates":
        "Civil Lengthy Chambers Available Dates.pdf",
    "family-lengthy-chambers-available-dates":
        "Family Lengthy Chambers Available Dates.pdf",
    "bankruptcy-discharge": "Bankruptcy Discharge.pdf",
    "one-day-over-registrar-dates": "One Day & Over Registrar Dates.pdf",
    "pre-hearing-conference-available": "Pre Hearing Conference Available.pdf",
    "registrar-available-dates": "Registrar Available Dates.pdf",
    "booking-trials": "Booking Trials.pdf",
    "booking-lengthy-chambers": "Booking Lengthy Chambers.pdf",
    "booking-lengthy-assize-chambers": "Booking Lengthy Assize Chambers.pdf",
}


def quote(name):
    return name.replace(" ", "%20")


class FakeResponse(object):
    def __init__(self, status, content=b"", headers=None, url=""):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.url = url
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise scrape.requests.HTTPError(str(self.status_code))


class FakeSession(object):
    def __init__(self, files, index_html):
        self.files = files          # url -> (bytes, etag)
        self.index_html = index_html
        self.body_transfers = 0

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        headers = headers or {}
        if scrape.INDEX_URL in url:
            return FakeResponse(200, self.index_html.encode(),
                                {"Content-Type": "text/html"}, url)
        if url not in self.files:
            return FakeResponse(404, b"", {}, url)
        content, etag = self.files[url]
        lm = "Tue, 28 Jul 2026 00:03:45 GMT"
        if headers.get("If-None-Match") == etag:
            return FakeResponse(304, b"", {"ETag": etag,
                                           "Last-Modified": lm}, url)
        self.body_transfers += 1
        return FakeResponse(200, content, {
            "Content-Type": "application/pdf", "ETag": etag,
            "Last-Modified": lm}, url)


def check(label, got, want):
    if got == want:
        PASS.append(label)
        print("  ok    %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL  %s: got %r, want %r" % (label, got, want))


def cell(payload, matter, length):
    for row in payload["grid"]["grid"]:
        if row["matter"] == matter:
            for c in row["cells"]:
                if c["length"] == length:
                    return c
    return None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src or not os.path.isdir(src):
        print("Usage: python test_integration.py <dir-of-real-pdfs>")
        return 1

    # Map archived files back to their real URLs.
    files, links = {}, []
    for root, _d, fns in os.walk(src):
        for fn in fns:
            if not fn.lower().endswith(".pdf"):
                continue
            m = re.match(r"^[0-9a-f]{16}-(.+)\.pdf$", fn)
            slug = m.group(1) if m else fn[:-4]
            real = FILENAMES.get(slug)
            if not real:
                continue
            url = BASE + quote(real)
            with open(os.path.join(root, fn), "rb") as f:
                files[url] = (f.read(), '"%s:0"' % slug[:12])
            links.append('<a href="/supreme_court/scheduling/lists/'
                         'Vancouver/%s">%s</a>'
                         % (quote(real).replace("&", "&amp;"), real))

    index = "<html><body>%s</body></html>" % "".join(links)
    print("serving %d documents\n" % len(files))
    if len(files) != 31:
        print("expected 31, got %d. Check the source directory." % len(files))
        return 1

    tmp = tempfile.mkdtemp(prefix="vanint-")
    os.chdir(tmp)

    sess = FakeSession(files, index)
    scrape.make_session = lambda: sess
    scrape.REQUEST_DELAY = 0
    os.environ["FORCE_RUN"] = "1"

    # -- run 1, cold --------------------------------------------------
    print("run 1, cold")
    rc = scrape.main()
    check("exit 0", rc, 0)
    latest = scrape.load_json("data/latest.json", {})
    check("31 documents", latest["document_count"], 31)
    check("all first seen",
          latest["status_counts"].get("FIRST_SEEN"), 31)
    check("bodies transferred", sess.body_transfers, 31)

    check("grid present", "error" not in latest["grid"], True)
    check("15 cells", latest["grid"]["summary"]["cells_total"], 15)
    check("13 available", latest["grid"]["summary"]["cells_available"], 13)

    c = cell(latest, "Civil", "2 Days")
    check("civil 2-day earliest", c["earliest_date"], "2027-03-22")
    check("civil 2-day count", c["dates_offered"], 90)
    c = cell(latest, "MVA", "2 Days")
    check("mva 2-day count", c["dates_offered"], 188)
    check("6-15 civil is none", cell(latest, "Civil", "6-15 Days")["state"],
          "none")
    c45 = cell(latest, "Civil", "4-5 Days")
    check("qualifier carried through",
          c45["earliest_unqualified_date"], "2027-10-18")

    docs = {d["title"]: d for d in latest["documents"]}
    check("instruction doc not parsed",
          docs["Booking Trials"]["parse_status"], None)
    check("court's own timestamp surfaced",
          docs["2 Day Civil Trials"]["date_created"], "2026-07-27T17:03")
    check("ISO declared", latest["date_format"], "YYYY-MM-DD")

    # -- run 2, all 304 -----------------------------------------------
    print("\nrun 2, every document returns 304")
    before = sess.body_transfers
    rc = scrape.main()
    check("exit 0", rc, 0)
    latest2 = scrape.load_json("data/latest.json", {})
    check("no bodies transferred", sess.body_transfers - before, 0)
    check("all unchanged", latest2["status_counts"].get("UNCHANGED"), 31)

    check("grid still built from cache",
          latest2["grid"]["summary"]["cells_available"], 13)
    check("civil 2-day unchanged",
          cell(latest2, "Civil", "2 Days")["earliest_date"], "2027-03-22")
    check("counts survive the 304 path",
          cell(latest2, "MVA", "2 Days")["dates_offered"], 188)
    check("qualifier survives the 304 path",
          cell(latest2, "Civil", "4-5 Days")["earliest_unqualified_date"],
          "2027-10-18")

    # -- run 3, one document genuinely changes ------------------------
    print("\nrun 3, the 2 day civil list loses its earliest date")
    url = BASE + quote("2 Day Civil Trials.pdf")

    # PDF text streams are compressed, so patching bytes does nothing.
    # Build a replacement in the court's own envelope format instead.
    original = scrape.load_json(
        "data/state.json", {})[url]
    old_dates = original["dates"]
    new_dates = old_dates[1:]          # the earliest date gets booked
    edited = make_bcsc_pdf("2 Days, Civil", new_dates,
                           "Tuesday, July 28, 2026 5:03 pm")
    check("fixture is a different file", edited != files[url][0], True)
    sess.files[url] = (edited, '"changed:0"')

    rc = scrape.main()
    latest3 = scrape.load_json("data/latest.json", {})
    d = {x["title"]: x for x in latest3["documents"]}["2 Day Civil Trials"]
    check("reported as UPDATED", d["status"], "UPDATED")
    check("new earliest date", d["earliest_date"], old_dates[1])
    check("previous earliest remembered",
          d["previous_earliest_date"], old_dates[0])
    check("one fewer date offered", d["dates_offered"], len(old_dates) - 1)
    check("grid follows",
          cell(latest3, "Civil", "2 Days")["earliest_date"], old_dates[1])
    check("only that document moved",
          cell(latest3, "MVA", "2 Days")["earliest_date"], "2026-09-21")

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
