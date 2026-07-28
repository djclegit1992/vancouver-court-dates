#!/usr/bin/env python3
"""
Courtready Vancouver Court Dates Finder
Tier two: read the dates out of a BCSC scheduling PDF.

These documents are machine-generated and share one envelope:

    <preamble prose>
    Vancouver
    Supreme Court
    Available court date(s): 2 Days, Civil
    Date Created: Monday, July 27, 2026 5:03 pm
    [optional column header: "Date" or "Date AM PM"]
    <rows>

Three things about the rows are not obvious and all three will produce
silently wrong output if missed:

  1. A second slot on the same day OMITS the date and inherits it from
     the row above. 98 of 213 PTC rows do this. Line-by-line parsing
     without carry-forward discards nearly half the availability.

  2. Where a row has no explicit time, morning versus afternoon is
     encoded ONLY in the x-position of the value under the AM or PM
     column header. SCA rows can carry both, meaning one date is two
     separate slots of different lengths.

  3. Trial rows can carry a qualifier such as "(4 day hearings only)",
     which means the date is not usable for the full length band.

Safety rule, from the playbook: a confident wrong answer is worse than
an admitted gap. The envelope is what lets us tell the difference
between "the court is offering nothing" and "we could not read this".
Envelope present with no rows is NO_DATES. Envelope absent is
UNREADABLE. We never infer one from the other.

All dates are normalised to YYYY-MM-DD. All times to 24-hour HH:MM.
Times are Pacific, the court's own timezone.
"""

import io
import re
from datetime import date, datetime

import pdfplumber

# --------------------------------------------------------------------------

MONTHS_ABBR = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

MONTHS_FULL = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}

DOW = r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day"

# "Monday, 22 Mar 2027"
RE_DATE_A = re.compile(
    r"\b%s,\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})" % DOW)

# "Tuesday, August 4, 2026"
RE_DATE_B = re.compile(
    r"\b%s,\s+([A-Z][a-z]{2,})\s+(\d{1,2}),\s+(\d{4})" % DOW)

RE_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)\b", re.I)
RE_ANNOT = re.compile(r"\(([^)]+)\)")
RE_DURATION = re.compile(
    r"\b(?:(\d+)\s*hours?)?\s*(?:(\d+)\s*minutes?)?\b", re.I)

RE_CATEGORY = re.compile(r"Available court date\(s\)\s*:\s*(.*)")
RE_CREATED = re.compile(r"Date Created:\s*(.+)")

# Column header midpoint fallback, if AM/PM headers cannot be located.
DEFAULT_COLUMN_SPLIT = 438.0

ROW_TOLERANCE = 3.0     # points; words within this share a row

PARSED, NO_DATES, UNREADABLE = "PARSED", "NO_DATES", "UNREADABLE"


# --------------------------------------------------------------------------

class ParseResult(object):
    def __init__(self):
        self.status = UNREADABLE
        self.reason = None
        self.location = None
        self.court = None
        self.category_raw = None
        self.length = None
        self.matter = None
        self.date_created = None      # ISO datetime string, court's own stamp
        self.slots = []               # list of dicts
        self.pages = 0

    @property
    def ok(self):
        return self.status in (PARSED, NO_DATES)

    @property
    def dates(self):
        """Distinct calendar dates offered, ISO, ascending."""
        return sorted(set(s["date"] for s in self.slots))

    @property
    def earliest(self):
        d = self.dates
        return d[0] if d else None

    def to_dict(self):
        return {
            "status": self.status,
            "reason": self.reason,
            "location": self.location,
            "category_raw": self.category_raw,
            "length": self.length,
            "matter": self.matter,
            "date_created": self.date_created,
            "pages": self.pages,
            "earliest_date": self.earliest,
            "distinct_dates": len(self.dates),
            "slot_count": len(self.slots),
            "slots": self.slots,
        }


# --------------------------------------------------------------------------

def _iso(y, m, d):
    return date(y, m, d).isoformat()


def _match_date(text):
    """Return ISO date from either format, or None."""
    m = RE_DATE_A.search(text)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
        if mon in MONTHS_ABBR:
            try:
                return _iso(int(year), MONTHS_ABBR[mon], int(day))
            except ValueError:
                return None
    m = RE_DATE_B.search(text)
    if m:
        mon, day, year = m.group(1), m.group(2), m.group(3)
        num = MONTHS_FULL.get(mon) or MONTHS_ABBR.get(mon[:3])
        if num:
            try:
                return _iso(int(year), num, int(day))
            except ValueError:
                return None
    return None


def _match_time(text):
    """Return 24-hour HH:MM and the am/pm marker, or (None, None)."""
    m = RE_TIME.search(text)
    if not m:
        return None, None
    hour, minute, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "pm" and hour != 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    return "%02d:%02d" % (hour, minute), ap


def _parse_created(text):
    """'Monday, July 27, 2026 5:03 pm' -> ISO datetime, court local."""
    d = _match_date(text)
    if not d:
        return None
    t, _ = _match_time(text)
    return "%sT%s" % (d, t) if t else d


def _rows_from_words(words):
    """Group words into visual rows by vertical position."""
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        placed = False
        for r in rows:
            if abs(r["top"] - w["top"]) <= ROW_TOLERANCE:
                r["words"].append(w)
                placed = True
                break
        if not placed:
            rows.append({"top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        r["text"] = " ".join(w["text"] for w in r["words"])
    rows.sort(key=lambda r: r["top"])
    return rows


def _column_split(words):
    """Midpoint between the AM and PM column headers, if present."""
    am = [w["x0"] for w in words if w["text"].strip() == "AM"]
    pm = [w["x0"] for w in words if w["text"].strip() == "PM"]
    if am and pm:
        return (min(am) + min(pm)) / 2.0
    return DEFAULT_COLUMN_SPLIT


def _period_of(word_x, split, has_columns):
    """Which column a value sits in. None when the file has no columns."""
    if not has_columns:
        return None
    return "am" if word_x < split else "pm"


def _duration_text(words):
    """Reassemble a duration phrase from a column's words."""
    txt = " ".join(w["text"] for w in words).strip()
    if not txt or txt.lower() == "available":
        return None
    if re.search(r"\b(hour|minute)", txt, re.I):
        return re.sub(r"\s+", " ", txt)
    return None


# --------------------------------------------------------------------------

def parse_pdf(data, expect_location="Vancouver"):
    """
    Parse one scheduling PDF.

    `data` is bytes. `expect_location` is validated against the body,
    per the playbook rule that every page is checked against the page
    we asked for.
    """
    res = ParseResult()

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            res.pages = len(pdf.pages)
            pages_words = [p.extract_words() for p in pdf.pages]
            full_text = "\n".join(
                (p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        res.reason = "pdfplumber failed: %s: %s" % (type(e).__name__, e)
        return res

    if not full_text.strip():
        res.reason = "no extractable text"
        return res

    # -- envelope ---------------------------------------------------
    cat = RE_CATEGORY.search(full_text)
    created = RE_CREATED.search(full_text)

    if not cat or not created:
        missing = []
        if not cat:
            missing.append("'Available court date(s):'")
        if not created:
            missing.append("'Date Created:'")
        res.reason = "envelope incomplete, missing %s" % " and ".join(missing)
        return res

    res.category_raw = cat.group(1).strip()
    res.date_created = _parse_created(created.group(1))

    parts = [p.strip() for p in res.category_raw.split(",")]
    res.length = parts[0] or None
    res.matter = parts[1] if len(parts) > 1 and parts[1] else None

    if re.search(r"^\s*Supreme Court\s*$", full_text, re.M):
        res.court = "Supreme Court"
    if re.search(r"^\s*%s\s*$" % re.escape(expect_location),
                 full_text, re.M):
        res.location = expect_location
    else:
        res.reason = ("body does not name %s; wrong document served"
                      % expect_location)
        return res

    # -- rows -------------------------------------------------------
    all_words = [w for pw in pages_words for w in pw]
    has_columns = bool([w for w in all_words if w["text"].strip() == "AM"])
    split = _column_split(all_words)

    current_date = None
    slots = []

    # The envelope appears on page one only. Continuation pages begin
    # straight into data rows, so this flag must persist across pages.
    # Resetting it per page silently discards every page after the
    # first, which on the 2-day civil list is two thirds of the dates.
    header_seen = False

    for words in pages_words:
        rows = _rows_from_words(words)

        for row in rows:
            text = row["text"]

            # Skip everything up to and including the envelope.
            if not header_seen:
                if RE_CREATED.search(text):
                    header_seen = True
                continue

            if re.match(r"^\s*Date(\s+AM)?(\s+PM)?\s*$", text):
                continue
            if re.match(r"^\s*\d+\s*$", text):     # page number
                continue

            row_date = _match_date(text)
            if row_date:
                current_date = row_date
            elif not RE_TIME.search(text):
                # Neither a date nor a time. Not a data row.
                continue

            if not current_date:
                continue

            time24, ap = _match_time(text)
            annot = RE_ANNOT.search(text)

            # Values sitting under the AM / PM columns.
            col_words = [w for w in row["words"]
                         if w["x0"] >= (split - 120)]
            am_words = [w for w in col_words if w["x0"] < split]
            pm_words = [w for w in col_words if w["x0"] >= split]

            entries = []
            if has_columns:
                for side, ws in (("am", am_words), ("pm", pm_words)):
                    if not ws:
                        continue
                    entries.append({
                        "period": side,
                        "duration": _duration_text(ws),
                        "marker": "Available" if any(
                            w["text"].strip().lower() == "available"
                            for w in ws) else None,
                    })
            if not entries:
                entries = [{"period": ap, "duration": None, "marker": None}]

            for e in entries:
                slots.append({
                    "date": current_date,
                    "time": time24,
                    "period": e["period"] or ap,
                    "duration": e["duration"],
                    "note": annot.group(1) if annot else None,
                })

    res.slots = slots
    res.status = PARSED if slots else NO_DATES
    if not slots:
        res.reason = "envelope intact, court is offering no dates"
    return res


def parse_file(path, expect_location="Vancouver"):
    with open(path, "rb") as f:
        return parse_pdf(f.read(), expect_location)
