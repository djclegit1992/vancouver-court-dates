#!/usr/bin/env python3
"""
Courtready Vancouver Court Dates Finder
Alert sender.

Runs in the same workflow job as the scraper, immediately after it.
Reads latest.json, works out which subscriptions are now satisfied,
sends one email each, and closes those rows.

The shape of a subscription:

    hearing_code   the document slug, e.g. '3-day-civil-trials'
    wanted_by      optional date. NULL means "any date at all".
    status         'active' until notified, then 'notified'
    notified_at    set the moment the email is accepted by Postmark

One email per subscription, then the row closes. That single rule is
what makes every other guardrail unnecessary: no caps, no rate limits,
no unsubscribe machinery. Someone who subscribes to all 28 lists gets
28 emails, ever.

Safety properties, in order of how much damage their absence would do:

  * Refuses to act on a stale latest.json. A run that failed leaves the
    previous file in place, and emailing from it would tell people about
    availability that may have moved hours ago.
  * latest.json only exists for healthy runs. A quarantined run writes
    quarantined.json instead, so this never sees it.
  * Sends first, marks second. If the process dies between the two, the
    worst case is a duplicate rather than a subscriber who waited for an
    email that was silently marked sent.
  * Dry run by default unless credentials are present, so running it by
    hand cannot email anyone.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# The court's own timezone. Everything shown to a subscriber is stated
# in it, because the dates being discussed are Vancouver court dates
# and a reader in Toronto or London does not want them shifted.
COURT_TZ = ZoneInfo("America/Vancouver")

# --------------------------------------------------------------------------

JURISDICTION = "BC"
LOCATION_CODE = "VA"
LOCATION_NAME = "Vancouver"

LATEST = os.path.join("data", "latest.json")

# The court rebuilds hourly and we check hourly, so anything older than
# this means a run failed and we are working from yesterday's picture.
MAX_AGE_MINUTES = 75

# A trigger fires once and does not retry. If Postmark blips, someone
# signs up and hears nothing. This sweeps those up.
CONFIRM_BACKSTOP_MINUTES = 60

REPLY_TO = "admin@courtready.ca"
TOOL_URL = "https://courtready.ca/vancouver-court-dates-finder/"
TOOL_NAME = "Courtready.ca's Vancouver Court Dates Finder"
CONTACT_NAME = "Tom Macintosh Zheng"
CONTACT_EMAIL = "tom@courtready.ca"
BOOK_URL = "https://justice.gov.bc.ca/scjob/"
PHONE = "604.660.2853"
COURT_URL = "https://www.bccourts.ca/supreme_court/scheduling/index.aspx"

# Hearing groups the court's online booking portal actually covers.
BOOKABLE_GROUPS = ("Trials", "Chambers")
BOOKABLE_SLUGS = (
    "case-planning-conference-available-dates",
    "judicial-case-conference-available-dates",
    "trial-management-conference-available-dates",
)

POSTMARK_URL = "https://api.postmarkapp.com/email"
TIMEOUT = 30


def env(name, default=None):
    v = os.environ.get(name)
    if v:
        return v.strip()
    return default


SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_KEY = env("SUPABASE_SERVICE_KEY")
POSTMARK_TOKEN = env("POSTMARK_TOKEN")
POSTMARK_STREAM = env("POSTMARK_STREAM", "outbound")
# Must be a verified sender on the Vancouver Postmark server.
# A mismatch makes Postmark reject every send.
FROM_ADDRESS = env("POSTMARK_FROM",
                   "Courtready <alerts@courtready.ca>")
DRY_RUN = env("ALERTS_DRY_RUN") == "1"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(msg)


def weekday_of(iso):
    try:
        return date.fromisoformat(iso).strftime("%A")
    except (ValueError, TypeError):
        return ""


def plural(n, one, many):
    return one if n == 1 else many


def court_time(iso):
    """
    Format a UTC timestamp in the court's local time.

    generated_at is UTC. Printing it unconverted while labelling it
    'Vancouver time' is worse than printing nothing: it tells the
    reader the data is seven hours fresher than it is.
    """
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(COURT_TZ)
    hour = local.hour % 12
    if hour == 0:
        hour = 12
    ampm = "am" if local.hour < 12 else "pm"
    return "%s, %d:%02d%s" % (local.strftime("%Y-%m-%d"), hour,
                              local.minute, ampm)


# --------------------------------------------------------------------------
# Supabase
# --------------------------------------------------------------------------

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer %s" % SUPABASE_KEY,
        "Content-Type": "application/json",
    }


def fetch_subscriptions():
    """Active, un-notified subscriptions for this location."""
    url = "%s/rest/v1/court_alerts" % SUPABASE_URL
    params = {
        "select": "*",
        "jurisdiction": "eq.%s" % JURISDICTION,
        "location_code": "eq.%s" % LOCATION_CODE,
        "status": "eq.active",
        "notified_at": "is.null",
        "order": "created_at.asc",
    }
    r = requests.get(url, headers=sb_headers(), params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_unconfirmed():
    """Subscriptions whose confirmation email never went out."""
    cutoff = (now_utc()
              - timedelta(minutes=CONFIRM_BACKSTOP_MINUTES)).isoformat()
    url = "%s/rest/v1/court_alerts" % SUPABASE_URL
    params = {
        "select": "*",
        "jurisdiction": "eq.%s" % JURISDICTION,
        "location_code": "eq.%s" % LOCATION_CODE,
        "status": "eq.active",
        "confirmation_sent_at": "is.null",
        "created_at": "lt.%s" % cutoff,
        "order": "created_at.asc",
    }
    r = requests.get(url, headers=sb_headers(), params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def mark(row_id, fields):
    url = "%s/rest/v1/court_alerts" % SUPABASE_URL
    h = dict(sb_headers())
    h["Prefer"] = "return=minimal"
    r = requests.patch(url, headers=h, params={"id": "eq.%d" % row_id},
                       json=fields, timeout=TIMEOUT)
    r.raise_for_status()


# --------------------------------------------------------------------------
# Postmark
# --------------------------------------------------------------------------

def send_email(to, subject, body, html=None):
    if DRY_RUN:
        log("      DRY RUN, not sending")
        return True, "dry-run"
    payload = {
        "From": FROM_ADDRESS,
        "To": to,
        "ReplyTo": REPLY_TO,
        "Subject": subject,
        "TextBody": body,
        "MessageStream": POSTMARK_STREAM,
    }
    if html:
        payload["HtmlBody"] = html
    r = requests.post(POSTMARK_URL, json=payload, timeout=TIMEOUT, headers={
        "X-Postmark-Server-Token": POSTMARK_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    if r.status_code == 200:
        return True, "sent"
    # 406 is Postmark's "recipient is suppressed", which is a normal
    # outcome rather than a fault: they unsubscribed or hard bounced.
    try:
        detail = r.json().get("Message", r.text[:200])
    except ValueError:
        detail = r.text[:200]
    return False, "HTTP %d: %s" % (r.status_code, detail)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def qualifies(sub, doc):
    """
    Is this subscription now satisfied?

    Returns (bool, matching_date_or_None, reason).

    A NULL wanted_by means any date at all, which is how the empty-list
    case works. A date means the earliest offering must fall on or
    before it.
    """
    if doc is None:
        return False, None, "no such document in the current payload"

    if doc.get("parse_status") == "UNREADABLE":
        return False, None, "document could not be read this run"

    dates = doc.get("dates") or []
    if not dates:
        return False, None, "still no dates offered"

    wanted = sub.get("wanted_by")
    if not wanted:
        return True, dates[0], "any date wanted, earliest is %s" % dates[0]

    # dates are ISO and sorted, so a string comparison is a date
    # comparison. Find the earliest that lands on or before the
    # threshold.
    for d in dates:
        if d <= wanted:
            return True, d, "wanted by %s, found %s" % (wanted, d)
    return False, None, "earliest is %s, wanted by %s" % (dates[0], wanted)


def booking_line(doc, group):
    if booking_online(doc, group):
        return ("To book, use the court's online booking system at %s, "
                "or call Supreme Court Scheduling on %s."
                % (BOOK_URL, PHONE))
    return "To book, call Supreme Court Scheduling on %s." % PHONE


def esc_html(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


P = ('style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#2f2f2f;'
     "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,"
     'sans-serif"')
A = 'style="color:#c87040;"'


def alert_html(sub, doc, matched, checked_at):
    """
    HTML version of the alert.

    A bare URL in a plain-text email is mangled by Outlook SafeLinks
    into an unreadable blob of tracking parameters. Anchor text is
    wrapped invisibly instead, so the reader sees a sentence.
    """
    name = esc_html(sub.get("hearing_name") or doc.get("title"))
    dates = doc.get("dates") or []
    n = len(dates)
    wd = weekday_of(matched)
    when = "%s%s" % (matched, " (%s)" % wd if wd else "")

    p = []
    p.append('<p %s>The Supreme Court is now offering a date for '
             '<strong>%s</strong> in Vancouver.</p>' % (P, name))
    if sub.get("wanted_by"):
        p.append('<p %s>You asked to hear when a date opened on or before '
                 '%s.</p>' % (P, esc_html(sub["wanted_by"])))
    p.append('<p %s>Earliest date that works: <strong>%s</strong>'
             % (P, esc_html(when)))
    if n > 1:
        p.append('<br>%d %s currently on offer for this hearing type.'
                 % (n, plural(n, "date is", "dates are")))
    p.append('</p>')

    if booking_online(doc, sub.get("hearing_group") or ""):
        p.append('<p %s>To book, use the court\u2019s '
                 '<a href="%s" %s>online booking system</a>, or call '
                 'Supreme Court Scheduling on %s.</p>'
                 % (P, BOOK_URL, A, PHONE))
    else:
        p.append('<p %s>To book, call Supreme Court Scheduling on %s.</p>'
                 % (P, PHONE))

    p.append('<p %s>We checked at %s Vancouver time. The court can change '
             'this list at any moment, so please confirm the date before '
             'you rely on it.</p>' % (P, esc_html(checked_at)))
    p.append('<p %s>This is the one email you asked for, and your alert is '
             'now closed.</p>' % P)
    p.append('<p %s>Questions: %s, <a href="mailto:%s" %s>%s</a></p>'
             % (P, CONTACT_NAME, CONTACT_EMAIL, A, CONTACT_EMAIL))
    p.append('<hr style="border:none;border-top:1px solid #e5e1db;'
             'margin:22px 0 14px;">')
    p.append('<p style="margin:0;font-size:13px;line-height:1.6;'
             "color:#857a72;font-family:-apple-system,BlinkMacSystemFont,"
             "'Segoe UI',Arial,sans-serif\">"
             '<a href="%s" %s>%s</a><br>'
             '<a href="%s" %s>The court\u2019s own scheduling page</a><br>'
             'Courtready.ca is an independent organisation and is not '
             'affiliated with the court.</p>'
             % (TOOL_URL, A, TOOL_NAME, COURT_URL, A))
    return '<div style="max-width:560px;">%s</div>' % "\n".join(p)


def booking_online(doc, group):
    if group in BOOKABLE_GROUPS:
        return True
    if doc.get("slug") in BOOKABLE_SLUGS:
        return True
    return False


def alert_body(sub, doc, matched, checked_at):
    name = sub.get("hearing_name") or doc.get("title")
    dates = doc.get("dates") or []
    n = len(dates)
    wd = weekday_of(matched)

    lines = []
    lines.append("The Supreme Court is now offering a date for %s in "
                 "Vancouver." % name)
    lines.append("")
    if sub.get("wanted_by"):
        lines.append("You asked to hear when a date opened on or before %s."
                     % sub["wanted_by"])
        lines.append("")
    lines.append("Earliest date that works: %s%s"
                 % (matched, " (%s)" % wd if wd else ""))
    if n > 1:
        lines.append("%d %s currently on offer for this hearing type."
                     % (n, plural(n, "date is", "dates are")))
    lines.append("")
    lines.append(booking_line(doc, sub.get("hearing_group") or ""))
    lines.append("")
    lines.append("Questions: %s, %s" % (CONTACT_NAME, CONTACT_EMAIL))
    lines.append("")
    lines.append(TOOL_NAME)
    lines.append(TOOL_URL)
    lines.append("")
    lines.append("We checked at %s Vancouver time. The court can change "
                 "this list at any moment, so please confirm the date "
                 "before you rely on it." % checked_at)
    lines.append("")
    lines.append("This is the one email you asked for, and your alert is "
                 "now closed.")
    lines.append("")
    lines.append("Courtready.ca is an independent organisation and is not "
                 "affiliated with the court.")
    return "\n".join(lines)


def confirm_body(sub, doc):
    name = sub.get("hearing_name") or (doc or {}).get("title") or \
        sub.get("hearing_code")
    lines = []
    if sub.get("wanted_by"):
        lines.append("We'll email you once, when the Supreme Court next "
                     "offers a date for %s in Vancouver on or before %s."
                     % (name, sub["wanted_by"]))
    else:
        lines.append("We'll email you once, when the Supreme Court next "
                     "offers any date for %s in Vancouver." % name)
    lines.append("")
    if doc:
        dates = doc.get("dates") or []
        if dates:
            lines.append("Right now the earliest date on that list is %s."
                         % dates[0])
        else:
            lines.append("Right now that list has no dates on it at all.")
    lines.append("We check every hour through the court's working day.")
    lines.append("")
    lines.append("To book, call Supreme Court Scheduling on %s." % PHONE)
    lines.append("")
    lines.append("See the full list: %s" % TOOL_URL)
    lines.append("")
    lines.append("Courtready.ca is an independent organisation and is not "
                 "affiliated with the court.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_latest():
    if not os.path.exists(LATEST):
        return None, "no %s; the run may have been quarantined" % LATEST
    try:
        with open(LATEST, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        return None, "could not parse %s: %s" % (LATEST, e)

    try:
        gen = datetime.fromisoformat(data["generated_at"])
    except (KeyError, ValueError) as e:
        return None, "no usable generated_at: %s" % e

    age = (now_utc() - gen).total_seconds() / 60.0
    if age > MAX_AGE_MINUTES:
        return None, ("latest.json is %.0f minutes old, limit is %d. "
                      "Refusing to send from stale data."
                      % (age, MAX_AGE_MINUTES))
    if data.get("healthy") is False:
        return None, "run was not healthy"
    return data, "%.0f minutes old" % age


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("alerts: SUPABASE_URL or SUPABASE_SERVICE_KEY not set, "
            "nothing to do")
        return 0
    if not POSTMARK_TOKEN:
        if not DRY_RUN:
            log("alerts: POSTMARK_TOKEN not set, nothing to do")
            return 0

    data, why = load_latest()
    if data is None:
        log("alerts: %s" % why)
        return 0
    log("alerts: latest.json %s" % why)

    docs = {}
    for d in data.get("documents", []):
        if d.get("slug"):
            docs[d["slug"]] = d

    stamp = court_time(data.get("generated_at", ""))

    # -- alerts ------------------------------------------------------
    try:
        subs = fetch_subscriptions()
    except requests.RequestException as e:
        log("alerts: could not read subscriptions: %s" % e)
        return 1

    log("alerts: %d active subscription(s)" % len(subs))
    sent = skipped = failed = 0

    for sub in subs:
        slug = sub.get("hearing_code")
        doc = docs.get(slug)
        ok, matched, reason = qualifies(sub, doc)
        who = "%s -> %s" % (sub.get("email"), slug)

        if not ok:
            skipped += 1
            log("   skip  %-52s %s" % (who, reason))
            continue

        subject = "A date has opened for %s" % (
            sub.get("hearing_name") or slug)
        body = alert_body(sub, doc, matched, stamp)
        html = alert_html(sub, doc, matched, stamp)

        good, detail = send_email(sub["email"], subject, body, html)
        if not good:
            failed += 1
            log("   FAIL  %-52s %s" % (who, detail))
            continue

        # Sent first, marked second, deliberately. A duplicate is a
        # smaller harm than a subscriber marked notified who never
        # received anything.
        try:
            mark(sub["id"], {
                "status": "notified",
                "notified_at": now_utc().isoformat(),
                "note": "matched %s" % matched,
            })
        except requests.RequestException as e:
            log("   SENT BUT NOT MARKED: id=%s %s -- %s"
                % (sub["id"], who, e))
            log("   This row will email again next run. Fix it by hand.")
            failed += 1
            continue

        sent += 1
        log("   sent  %-52s %s" % (who, reason))

    # -- confirmation backstop ---------------------------------------
    confirmed = 0
    try:
        pending = fetch_unconfirmed()
    except requests.RequestException as e:
        log("alerts: could not check unconfirmed: %s" % e)
        pending = []

    if pending:
        log("alerts: %d signup(s) never got a confirmation, sweeping up"
            % len(pending))
    for sub in pending:
        doc = docs.get(sub.get("hearing_code"))
        subject = "You're on the list for %s" % (
            sub.get("hearing_name") or sub.get("hearing_code"))
        good, detail = send_email(sub["email"], subject,
                                  confirm_body(sub, doc))
        if not good:
            log("   FAIL  confirmation to %s: %s" % (sub.get("email"), detail))
            continue
        try:
            mark(sub["id"], {"confirmation_sent_at": now_utc().isoformat()})
            confirmed += 1
        except requests.RequestException as e:
            log("   confirmation sent but not marked: id=%s %s"
                % (sub["id"], e))

    log("alerts: %d sent, %d not yet due, %d failed, %d confirmations "
        "swept" % (sent, skipped, failed, confirmed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
