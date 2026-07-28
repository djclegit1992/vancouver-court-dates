#!/usr/bin/env python3
"""
Courtready Vancouver Court Dates Finder
Tier one: document watcher for BCSC Vancouver scheduling lists.

Reads the scheduling index once, collects Vancouver's document links
verbatim, and checks each one with a conditional request. Detects
replacement without parsing any dates.

Design notes that matter:

  * Links are never constructed. Every URL is an href read off the index
    and resolved against the page's own base.
  * Two hashes per document. bytes_hash answers "was the file object
    replaced", text_hash answers "did what it says change". PDFs exported
    from Word carry a fresh /CreationDate every save, so bytes alone
    produces false change events.
  * A document we could not read is UNREADABLE, never "no dates".
  * A bad run is saved but does not update latest.json.
"""

import csv
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# Prefer the operating system's trust store over certifi's bundle.
#
# On Windows, curl.exe uses the OS store while requests uses certifi,
# so the two disagree in two common situations: a server that omits an
# intermediate certificate (Windows fetches it via AIA, OpenSSL does
# not), and local TLS interception by a corporate proxy or antivirus
# (its root is in the OS store, never in certifi).
#
# truststore makes Python trust exactly what the browser and curl
# trust. If it is not installed we fall through to certifi, which is
# the normal working case on a Linux CI runner. Verification is never
# disabled.
try:
    import truststore
    truststore.inject_into_ssl()
    TRUST_STORE = "os"
except Exception:
    TRUST_STORE = "certifi"

# Some servers omit an intermediate certificate and rely on the client
# to fetch it via the AIA extension. Windows and browsers do that
# silently; OpenSSL does not, so Python fails on a site that works
# perfectly in a browser. This repairs the chain if, and only if, it is
# currently broken. Verification is never disabled: the fetched
# intermediate still has to chain to an already-trusted root.
TLS_REPAIR = "not attempted"


def ensure_tls(host):
    global TLS_REPAIR
    try:
        import tlsfix
        TLS_REPAIR = tlsfix.ensure(host)
    except Exception as e:
        TLS_REPAIR = "skipped: %s" % e
    return TLS_REPAIR

# --------------------------------------------------------------------------
# Per-jurisdiction constants. See playbook section 11.
# --------------------------------------------------------------------------

CONTACT_EMAIL = "admin@courtready.ca"
PROJECT_URL = "https://courtready.ca"
USER_AGENT = "CourtreadyBot/1.0 (+%s; %s)" % (PROJECT_URL, CONTACT_EMAIL)

BASE = "https://www.bccourts.ca"
INDEX_URL = "https://www.bccourts.ca/supreme_court/scheduling/index.aspx"

LOCATION_NAME = "Vancouver"
JURISDICTION = "BC"
LOCATION_CODE = "VA"

# A link belongs to us if its resolved path contains this fragment.
# Matched case-insensitively: the site uses both /Supreme_Court/ and
# /supreme_court/, and both /vancouver/ and /Vancouver/.
LOCATION_PATH_FRAGMENT = "/scheduling/lists/vancouver/"

# Expected document count. Confirmed at 31 on 27 July 2026.
# A drop below this floor quarantines the run.
MIN_DOCUMENTS = 25

REQUEST_DELAY = 2.0
REQUEST_TIMEOUT = 45
RETRIES = 3
SKIP_GUARD_MINUTES = 20
# Bump when the shape of a cached parse record changes. Entries written
# by an older version are discarded and re-derived rather than trusted.
# Without this, adding a field to the cache is a silent no-op: existing
# entries never expire, so the new field never appears for any document
# that has not changed since, and code depending on it quietly does
# nothing.
PARSE_SCHEMA = 2

FAILURE_STREAK_LIMIT = 3
MAX_FAILURE_RATIO = 0.20
STALE_MONTHS = 3

DATA = "data"
RAW = os.path.join(DATA, "raw")
RUNS = os.path.join(DATA, "runs")
PARSED = os.path.join(DATA, "parsed")

# Documents that explain how to book rather than list availability.
# Watched for change, never rendered as a wait time.
INSTRUCTION_TITLES = {
    "booking trials",
    "booking lengthy chambers",
    "booking lengthy assize chambers",
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


def norm(s):
    """Whitespace-insensitive, case-insensitive comparison key."""
    return " ".join((s or "").split()).upper()


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower())
    return s.strip("-")[:80]


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def title_from_url(url):
    """Filename, percent-decoded, extension stripped."""
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    return re.sub(r"\.(pdf|docx?|xlsx?)$", "", name, flags=re.I).strip()


def kind_from_url(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
    return ext or "unknown"


def ensure_dirs():
    for d in (DATA, RAW, RUNS, PARSED):
        os.makedirs(d, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Link collection. Site-specific. See playbook section 3.1.
# --------------------------------------------------------------------------

def collect_links(html, index_url):
    """
    Return Vancouver's documents as dicts of title, url, kind.

    Selection is by resolved URL path, not by DOM position. The index
    carries all thirty locations in one page and its section markup is
    not something we want to depend on. Path matching survives a redesign
    of the page; a div selector does not.
    """
    soup = BeautifulSoup(html, "lxml")
    found = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue

        url = urljoin(index_url, href)
        path = urlparse(url).path

        if LOCATION_PATH_FRAGMENT not in path.lower():
            continue

        # Anchor text is often blank or an image; the filename is
        # the reliable label on this site.
        title = title_from_url(url)
        if not title:
            continue

        # Deduplicate on the URL, keeping first occurrence.
        if url not in found:
            found[url] = {
                "title": title,
                "url": url,
                "kind": kind_from_url(url),
                "is_instruction": norm(title).lower() in INSTRUCTION_TITLES,
            }

    return sorted(found.values(), key=lambda d: d["title"].lower())


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "From": CONTACT_EMAIL,
        "Accept": "*/*",
    })
    return s


def fetch(session, url, headers=None):
    """Fetch with retries. Returns the response or raises."""
    last = None
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=headers or {},
                            timeout=REQUEST_TIMEOUT, allow_redirects=True)
            return r
        except requests.RequestException as e:
            last = e
            if attempt < RETRIES - 1:
                time.sleep(REQUEST_DELAY * (attempt + 2))
    raise last


def extract_text(content, kind):
    """
    Normalised text for text_hash. Returns None when unreadable.

    An empty extraction counts as unreadable, not as readable-and-blank.
    Court PDFs assembled by hand are sometimes scans or image-only
    exports, and those yield "". Hashing "" would give every such
    document an identical text_hash, so a real content change would
    read as REEXPORTED and never alert. Silent, and the worst kind.
    """
    if kind != "pdf":
        return None
    try:
        import pdfplumber
        import io
        chunks = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
        text = " ".join(" ".join(chunks).split())
        return text if text else None
    except Exception:
        return None


def check_document(session, doc, prev):
    """
    Conditional fetch of one document.

    Returns a record with a status drawn from:
      UNCHANGED   304, or 200 with identical bytes
      UPDATED     200, extracted text differs
      REEXPORTED  200, bytes differ but text identical
      FIRST_SEEN  no prior record
      FETCH_FAILED transport error, non-200/304, or wrong content type

    Readability is recorded separately from status. A document with no
    extractable text is still watched; change detection simply falls
    back to bytes and the page says so.
    """
    rec = {
        "title": doc["title"],
        "url": doc["url"],
        "kind": doc["kind"],
        "is_instruction": doc["is_instruction"],
        "checked_at": iso(now()),
        "status": None,
        "http_status": None,
        "bytes_hash": None,
        "text_hash": None,
        "size_bytes": None,
        "last_modified": None,
        "etag": None,
        "readable": None,
        "note": None,
    }

    cond = {}
    if prev:
        if prev.get("etag"):
            cond["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            cond["If-Modified-Since"] = prev["last_modified"]

    try:
        r = fetch(session, doc["url"], cond)
    except requests.RequestException as e:
        rec["status"] = "FETCH_FAILED"
        rec["note"] = type(e).__name__
        return rec

    rec["http_status"] = r.status_code

    if r.status_code == 304:
        rec["status"] = "UNCHANGED"
        for k in ("bytes_hash", "text_hash", "size_bytes",
                  "last_modified", "etag", "readable"):
            rec[k] = prev.get(k)
        # The archived copy is still on disk under the carried hash, so
        # a stale-schema cache entry can be re-derived without a fetch.
        rec["archive_path"] = archive_path(
            prev.get("text_hash") or prev.get("bytes_hash") or "", doc)
        return rec

    if r.status_code != 200:
        rec["status"] = "FETCH_FAILED"
        rec["note"] = "HTTP %d" % r.status_code
        return rec

    # Validate the body against what we asked for. A courtesy redirect to
    # an error page still returns 200 on many IIS sites.
    ctype = (r.headers.get("Content-Type") or "").lower()
    if doc["kind"] == "pdf" and "pdf" not in ctype:
        rec["status"] = "FETCH_FAILED"
        rec["note"] = "content-type %s, expected pdf" % (ctype or "none")
        return rec

    content = r.content
    rec["size_bytes"] = len(content)
    rec["bytes_hash"] = sha256(content)
    rec["last_modified"] = r.headers.get("Last-Modified")
    rec["etag"] = r.headers.get("ETag")

    text = extract_text(content, doc["kind"])
    rec["readable"] = text is not None
    rec["text_hash"] = sha256(text.encode("utf-8")) if text is not None else None
    rec["archive_path"] = archive(content, rec, doc)

    # Bytes are compared first, deliberately. An image-only PDF can never
    # be read, but it can still be seen to be unchanged. Checking
    # readability first would mark it UNREADABLE on every run forever,
    # and enough of those would quarantine the job permanently.
    if prev and prev.get("bytes_hash") == rec["bytes_hash"]:
        rec["status"] = "UNCHANGED"
        return rec

    if not prev:
        rec["status"] = "FIRST_SEEN"
        if text is None:
            rec["note"] = "no extractable text; change detection is byte-only"
        return rec

    # The file was replaced. Whether that matters depends on the text.
    if text is None or not prev.get("text_hash"):
        rec["status"] = "UPDATED"
        rec["note"] = ("file replaced; text could not be verified, "
                       "reporting as a change")
        return rec

    if prev["text_hash"] == rec["text_hash"]:
        rec["status"] = "REEXPORTED"
        rec["note"] = "file replaced, extracted text identical"
    else:
        rec["status"] = "UPDATED"

    return rec


def finalize_status(rec, prev):
    """
    Refine UPDATED versus REEXPORTED using the parsed content.

    check_document can only compare raw extracted text, which always
    differs after a regeneration because the court stamps its own
    "Date Created" time inside the document. Once the parser has run we
    can compare the availability itself, which is the only thing a
    subscriber cares about.

    Only ever downgrades UPDATED to REEXPORTED. It never promotes a
    quiet document to a noisy one, so a parser failure cannot invent an
    alert.
    """
    if rec.get("status") != "UPDATED" or not prev:
        return rec
    info = rec.get("parse") or {}
    new_hash = info.get("content_hash")
    old_hash = prev.get("content_hash")
    if new_hash and old_hash and new_hash == old_hash:
        rec["status"] = "REEXPORTED"
        rec["note"] = ("regenerated by the court; availability identical")
    return rec


def archive_path(digest, doc):
    d = os.path.join(RAW, digest[:2])
    return os.path.join(d, "%s-%s.%s" % (digest[:16], slugify(doc["title"]),
                                         doc["kind"]))


def parse_document(rec, doc, prev):
    """
    Tier two. Read the dates out of the document.

    Keyed on bytes_hash and cached, so a 304 costs nothing and an
    unchanged file is never re-parsed. A parse failure is recorded but
    never fails the fetch: fetching and reading are separate layers and
    conflating them would let a template change take the whole run down.
    """
    digest = rec.get("text_hash") or rec.get("bytes_hash")
    if not digest or rec["status"] == "FETCH_FAILED":
        return None

    # Cache keyed on content: a re-export with identical text reuses
    # the existing parse rather than repeating the work.
    cache = os.path.join(PARSED, "%s.json" % digest[:16])
    cached = load_json(cache, None)
    if cached and cached.get("schema") == PARSE_SCHEMA:
        return cached

    path = rec.get("archive_path") or archive_path(digest, doc)
    if not os.path.exists(path):
        return None

    try:
        import parse as parser
        with open(path, "rb") as f:
            result = parser.parse_pdf(f.read(), LOCATION_NAME)
        info = {
            "schema": PARSE_SCHEMA,
            "parse_status": result.status,
            "parse_reason": result.reason,
            "category_raw": result.category_raw,
            "length": result.length,
            "matter": result.matter,
            "date_created": result.date_created,
            "earliest_date": result.earliest,
            "dates_offered": len(result.dates),
            "slot_count": len(result.slots),
            "last_date": result.dates[-1] if result.dates else None,
            "notes": sorted(set(s["note"] for s in result.slots
                                if s["note"])),
            # Full sets. Needed for the grid's qualifier handling now,
            # and for date-level diffing in the change feed later.
            "dates": result.dates,
            "qualified_dates": sorted(set(
                s["date"] for s in result.slots if s["note"])),
            "unqualified_dates": sorted(set(
                s["date"] for s in result.slots if not s["note"])),
            # The hash that actually decides whether to alert.
            #
            # text_hash cannot do this job. The court prints its own
            # generation timestamp INSIDE the document body:
            #   "Date Created: Monday, July 27, 2026 5:03 pm"
            # so every regeneration changes the extracted text even when
            # not one date moved. An empty list with no dates in it at
            # all still comes back with different text. Alerting on that
            # would send a message per document per regeneration,
            # forever, and subscribers would learn to ignore us.
            #
            # This hashes the availability itself and nothing else.
            #
            # Only set when the document was genuinely read. An
            # unreadable file yields an empty slot list, and hashing
            # that would give every unreadable document an identical
            # value, so a real change would compare equal and go
            # silent. Absence of data must never masquerade as data.
            "content_hash": (sha256(json.dumps(
                sorted([(s["date"], s["time"], s["period"],
                         s["duration"], s["note"]) for s in result.slots],
                       key=lambda t: [x or "" for x in t]),
                sort_keys=True).encode("utf-8"))
                if result.ok else None),
        }
    except Exception as e:
        info = {
            "schema": PARSE_SCHEMA,
            "parse_status": "UNREADABLE",
            "parse_reason": "parser raised %s: %s" % (type(e).__name__, e),
            "earliest_date": None,
            "dates_offered": 0,
        }

    save_json(cache, info)
    return info


def archive(content, rec, doc):
    """
    Content-addressed archive. Raw bytes are the one thing that cannot
    be added retroactively: find a parser bug in month four and you can
    reprocess months one to four instead of losing them.

    Keyed on text_hash, not bytes_hash, when the document is readable.
    A PDF re-exported with no substantive edit gets fresh bytes but
    identical text, so keying on bytes would store a new copy every
    night. Over a year that is hundreds of megabytes of byte-different,
    word-identical files in a repository that has to stay clonable.
    Keying on content keeps exactly one copy per distinct version,
    which is what reprocessing actually needs.

    Unreadable files fall back to bytes_hash, since content is the one
    thing we cannot compare on those.
    """
    digest = rec.get("text_hash") or rec.get("bytes_hash")
    if not digest:
        return None
    path = archive_path(digest, doc)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)
    return path


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

def update_health(records):
    """Consecutive failure count per document. Three in a row is a break."""
    path = os.path.join(DATA, "health.json")
    health = load_json(path, {})
    broken = []

    for rec in records:
        key = rec["url"]
        if rec["status"] == "FETCH_FAILED":
            health[key] = health.get(key, 0) + 1
            if health[key] >= FAILURE_STREAK_LIMIT:
                broken.append((rec["title"], health[key]))
        else:
            health[key] = 0

    save_json(path, health)
    return broken


def check_catalogue(docs):
    """Any addition, removal or rename is reported."""
    path = os.path.join(DATA, "catalogue.json")
    known = load_json(path, {"titles": []})
    current = sorted(d["title"] for d in docs)
    previous = known.get("titles", [])

    added = [t for t in current if t not in previous]
    removed = [t for t in previous if t not in current]

    save_json(path, {"titles": current, "updated_at": iso(now())})
    return added, removed, bool(previous)


def skip_guard():
    """Exit fast if a run finished recently, unless forced."""
    if os.environ.get("FORCE_RUN") == "1":
        return False
    path = os.path.join(DATA, "latest.json")
    latest = load_json(path, None)
    if not latest:
        return False
    try:
        last = datetime.fromisoformat(latest["generated_at"])
    except (KeyError, ValueError):
        return False
    age = now() - last
    return age < timedelta(minutes=SKIP_GUARD_MINUTES)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def append_history(run_id, records):
    path = os.path.join(DATA, "history.csv")
    cols = ["run_id", "checked_at", "title", "url", "status", "http_status",
            "bytes_hash", "text_hash", "size_bytes", "last_modified", "etag"]
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        for rec in records:
            row = dict(rec)
            row["run_id"] = run_id
            w.writerow(row)


def build_state(records, prev_state, run_id):
    """Merge this run into the durable per-document state."""
    state = {}
    ts = iso(now())

    for rec in records:
        key = rec["url"]
        old = prev_state.get(key, {})
        entry = dict(old)
        entry.update({
            "title": rec["title"],
            "url": rec["url"],
            "kind": rec["kind"],
            "is_instruction": rec["is_instruction"],
            "status": rec["status"],
            "last_seen": ts,
            "note": rec["note"],
        })

        if rec["status"] not in ("FETCH_FAILED",):
            for k in ("bytes_hash", "text_hash", "size_bytes",
                      "last_modified", "etag"):
                if rec.get(k) is not None:
                    entry[k] = rec[k]

        # Readability is a property of this run, never inherited.
        # A file that stopped being readable must not keep reporting
        # readable because a previous run left a text_hash behind.
        # This is the same failure as rendering a failed fetch as
        # "no dates available": absence of data treated as data.
        if rec["status"] == "FETCH_FAILED":
            entry.setdefault("readable", None)   # unknown, we never got it
        else:
            entry["readable"] = rec.get("readable")
            if not rec.get("readable"):
                entry["text_hash"] = None

        info = rec.get("parse") or {}
        if info:
            prev_earliest = old.get("earliest_date")
            entry.update(info)
            if (prev_earliest and info.get("earliest_date")
                    and prev_earliest != info["earliest_date"]):
                entry["previous_earliest_date"] = prev_earliest

        entry.setdefault("first_seen", ts)
        if rec["status"] in ("UPDATED", "FIRST_SEEN"):
            entry["last_changed"] = ts
            entry["last_changed_run"] = run_id
        entry.setdefault("last_changed", ts)

        state[key] = entry

    return state


def build_grid(state):
    """
    The availability grid, assembled from cached parse data.

    Never fails the run. If the grid cannot be built the page falls back
    to the document list rather than showing nothing, and the error is
    carried in the payload so the failure is visible rather than silent.
    """
    try:
        import grid as gridmod
        info = {}
        for e in state.values():
            key = (e.get("matter"), e.get("length"))
            if key[0] in gridmod.MATTERS and key[1] in gridmod.LENGTHS:
                info[key] = e
        return gridmod.build_from_info(info)
    except Exception as e:
        return {"error": "grid unavailable: %s: %s" % (type(e).__name__, e)}


def build_latest(state, records, run_id, added, removed, healthy):
    """The payload the public page reads."""
    ts = now()
    docs = []

    for key, e in sorted(state.items(), key=lambda kv: kv[1]["title"].lower()):
        changed = e.get("last_changed")
        days = None
        if changed:
            try:
                days = (ts - datetime.fromisoformat(changed)).days
            except ValueError:
                days = None
        docs.append({
            "title": e["title"],
            "url": e["url"],
            "kind": e["kind"],
            "is_instruction": e.get("is_instruction", False),
            "status": e.get("status"),
            "last_changed": changed,
            "days_since_change": days,
            "court_last_modified": e.get("last_modified"),
            "size_bytes": e.get("size_bytes"),
            "possibly_stale": bool(days is not None and days > STALE_MONTHS * 30),
            "readable": e.get("readable"),
            "parse_status": e.get("parse_status"),
            "category": e.get("category_raw"),
            "date_created": e.get("date_created"),
            "earliest_date": e.get("earliest_date"),
            "previous_earliest_date": e.get("previous_earliest_date"),
            "dates_offered": e.get("dates_offered"),
            "last_date": e.get("last_date"),
            "notes": e.get("notes") or [],
            # The dates themselves. This is the content of the tool, so
            # it belongs in the public payload rather than only in
            # internal state. Roughly 1,500 across all lists, which is
            # a few tens of kilobytes.
            "dates": e.get("dates") or [],
            "qualified_dates": e.get("qualified_dates") or [],
        })

    counts = {}
    for rec in records:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1

    return {
        "run_id": run_id,
        "generated_at": iso(ts),
        "date_format": "YYYY-MM-DD",
        "timezone": "America/Vancouver",
        "grid": build_grid(state),
        "jurisdiction": JURISDICTION,
        "location_code": LOCATION_CODE,
        "location_name": LOCATION_NAME,
        "source_url": INDEX_URL,
        "healthy": healthy,
        "document_count": len(docs),
        "status_counts": counts,
        "catalogue_added": added,
        "catalogue_removed": removed,
        "documents": docs,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ensure_dirs()

    if skip_guard():
        print("skip guard: a run finished within %d minutes, exiting"
              % SKIP_GUARD_MINUTES)
        return 0

    run_id = now().strftime("%Y%m%dT%H%M%SZ")
    print("run %s" % run_id)
    print("tls: %s | %s" % (TRUST_STORE,
                            ensure_tls(urlparse(INDEX_URL).hostname)))
    session = make_session()

    # One request for the index.
    try:
        r = fetch(session, INDEX_URL)
        r.raise_for_status()
    except Exception as e:
        print("FATAL: could not load index: %s" % e, file=sys.stderr)
        return 1

    docs = collect_links(r.text, r.url)
    print("index: %d Vancouver documents" % len(docs))

    if len(docs) < MIN_DOCUMENTS:
        print("FATAL: found %d documents, floor is %d. Index markup or the "
              "URL scheme has probably changed. Not publishing."
              % (len(docs), MIN_DOCUMENTS), file=sys.stderr)
        return 1

    added, removed, had_catalogue = check_catalogue(docs)
    if had_catalogue and (added or removed):
        print("catalogue changed. added=%s removed=%s" % (added, removed))

    prev_state = load_json(os.path.join(DATA, "state.json"), {})

    records = []
    for i, doc in enumerate(docs):
        if i:
            time.sleep(REQUEST_DELAY)
        prev = prev_state.get(doc["url"])
        rec = check_document(session, doc, prev)
        if not doc["is_instruction"]:
            rec["parse"] = parse_document(rec, doc, prev)
            finalize_status(rec, prev)
        records.append(rec)
        flag = "" if rec["status"] in ("UNCHANGED", "REEXPORTED") else "  <-"
        print("  %-12s %s%s" % (rec["status"], rec["title"], flag))

    failures = sum(1 for r_ in records if r_["status"] == "FETCH_FAILED")
    ratio = failures / float(len(records)) if records else 1.0
    healthy = ratio <= MAX_FAILURE_RATIO

    broken = update_health(records)

    # Always save the run, healthy or not.
    with gzip.open(os.path.join(RUNS, "%s.json.gz" % run_id), "wt",
                   encoding="utf-8") as f:
        json.dump({"run_id": run_id, "records": records}, f, indent=2)
    append_history(run_id, records)

    state = build_state(records, prev_state, run_id)
    save_json(os.path.join(DATA, "state.json"), state)

    latest = build_latest(state, records, run_id, added, removed, healthy)

    if healthy:
        save_json(os.path.join(DATA, "latest.json"), latest)
        print("published. %d failures of %d" % (failures, len(records)))
    else:
        save_json(os.path.join(DATA, "quarantined.json"), latest)
        print("QUARANTINED: %d failures of %d exceeds %.0f%%. latest.json "
              "untouched, site keeps last good data."
              % (failures, len(records), MAX_FAILURE_RATIO * 100),
              file=sys.stderr)

    updated = [r_["title"] for r_ in records if r_["status"] == "UPDATED"]
    if updated:
        print("UPDATED: %s" % ", ".join(updated))

    if broken:
        for title, streak in broken:
            print("BROKEN: %s has failed %d consecutive runs"
                  % (title, streak), file=sys.stderr)
        return 1

    if not healthy:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
