#!/usr/bin/env python3
"""
Offline tests for the alert sender.

No network, no database, no Postmark. Fakes both HTTP endpoints and
walks every path that decides whether a real person gets an email.

The cases that matter most are the ones where NOT sending is correct:
a rebuilt-but-unchanged list, a threshold not yet met, a document that
could not be read, and stale data. A false alert is worse than a late
one, because it teaches people the tool is noise.

Usage:  python test_alerts.py <dir-of-real-pdfs>
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["SUPABASE_URL"] = "https://fake.supabase.co"
os.environ["SUPABASE_SERVICE_KEY"] = "fake-service-key"
os.environ["POSTMARK_TOKEN"] = "fake-postmark-token"

import alerts

PASS, FAIL = [], []


def check(label, got, want):
    if got == want:
        PASS.append(label)
        print("  ok    %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL  %s: got %r, want %r" % (label, got, want))


def checkTrue(label, got):
    check(label, bool(got), True)


# --------------------------------------------------------------------------

class World(object):
    """A fake Supabase table and a fake Postmark."""

    def __init__(self):
        self.rows = []
        self.next_id = 1
        self.emails = []
        self.postmark_status = 200
        self.patch_fails = False

    def add(self, email, slug, name=None, wanted_by=None, group="Trials",
            confirmed=True):
        row = {
            "id": self.next_id, "email": email, "hearing_code": slug,
            "hearing_name": name or slug, "hearing_group": group,
            "wanted_by": wanted_by, "status": "active",
            "notified_at": None,
            "confirmation_sent_at": (datetime.now(timezone.utc).isoformat()
                                     if confirmed else None),
            "created_at": (datetime.now(timezone.utc)
                           - timedelta(hours=3)).isoformat(),
        }
        self.rows.append(row)
        self.next_id += 1
        return row

    def get(self, row_id):
        for r in self.rows:
            if r["id"] == row_id:
                return r
        return None


WORLD = World()


class FakeResp(object):
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise alerts.requests.HTTPError(str(self.status_code))


def fake_get(url, headers=None, params=None, timeout=None):
    params = params or {}
    rows = [r for r in WORLD.rows if r["status"] == "active"]
    if params.get("notified_at") == "is.null":
        rows = [r for r in rows if r["notified_at"] is None]
    if params.get("confirmation_sent_at") == "is.null":
        rows = [r for r in rows if r["confirmation_sent_at"] is None]
    return FakeResp(200, rows)


def fake_patch(url, headers=None, params=None, json=None, timeout=None):
    if WORLD.patch_fails:
        return FakeResp(500, text="simulated")
    rid = int(params["id"].split(".")[1])
    row = WORLD.get(rid)
    if row:
        row.update(json or {})
    return FakeResp(204)


def fake_post(url, json=None, timeout=None, headers=None):
    if WORLD.postmark_status != 200:
        return FakeResp(WORLD.postmark_status,
                        {"Message": "simulated failure"})
    WORLD.emails.append(json)
    return FakeResp(200, {"MessageID": "fake"})


alerts.requests.get = fake_get
alerts.requests.patch = fake_patch
alerts.requests.post = fake_post


# --------------------------------------------------------------------------

def write_latest(payload, age_minutes=5, healthy=True):
    payload = dict(payload)
    payload["generated_at"] = (
        datetime.now(timezone.utc)
        - timedelta(minutes=age_minutes)).isoformat()
    payload["healthy"] = healthy
    os.makedirs("data", exist_ok=True)
    with open(alerts.LATEST, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def reset(payload, **kw):
    global WORLD
    WORLD = World()
    # rebind the fakes to the new world
    write_latest(payload, **kw)
    return WORLD


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src or not os.path.isdir(src):
        print("Usage: python test_alerts.py <dir-of-real-pdfs> "
          "[path-to-bc-index.html]")
        return 1
    src = os.path.abspath(src)

    tmp = tempfile.mkdtemp(prefix="vcdalert-")
    fixture = os.path.join(tmp, "fixture.json")

    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "make_fixture.py"), src, fixture]
    if len(sys.argv) > 2:
        cmd.append(os.path.abspath(sys.argv[2]))
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd())
    if not os.path.exists(fixture):
        print("could not build fixture:\n%s\n%s" % (r.stdout, r.stderr))
        return 1
    base = json.load(open(fixture, encoding="utf-8"))
    os.chdir(tmp)

    by_slug = {d["slug"]: d for d in base["documents"]}
    empty = [d for d in base["documents"]
             if not d["is_instruction"] and not d["dates"]][0]
    full = by_slug["2-day-mva-trials"]
    print("using: empty list %r, populated list %r earliest %s\n"
          % (empty["slug"], full["slug"], full["dates"][0]))

    # -- 1. empty list stays quiet ----------------------------------
    print("an empty list must not alert")
    w = reset(base)
    w.add("a@example.com", empty["slug"], empty["title"])
    alerts.main()
    check("no email sent", len(w.emails), 0)
    check("row still active", w.get(1)["status"], "active")

    # -- 2. no threshold, dates exist -------------------------------
    print("\nno threshold, list has dates")
    w = reset(base)
    w.add("b@example.com", full["slug"], full["title"])
    alerts.main()
    check("one email sent", len(w.emails), 1)
    check("row closed", w.get(1)["status"], "notified")
    checkTrue("notified_at set", w.get(1)["notified_at"])
    body = w.emails[0]["TextBody"]
    checkTrue("names the earliest date", full["dates"][0] in body)
    checkTrue("says the alert is now closed", "now closed" in body)
    checkTrue("independent-organisation line present",
              "independent organisation" in body)
    checkTrue("offers online booking for a trial",
              alerts.BOOK_URL in body)

    # -- 3. threshold not yet met -----------------------------------
    print("\nthreshold earlier than anything on offer")
    w = reset(base)
    w.add("c@example.com", full["slug"], full["title"],
          wanted_by="2026-01-01")
    alerts.main()
    check("no email sent", len(w.emails), 0)
    check("row still active", w.get(1)["status"], "active")

    # -- 4. threshold met -------------------------------------------
    print("\nthreshold comfortably met")
    w = reset(base)
    w.add("d@example.com", full["slug"], full["title"],
          wanted_by="2027-12-31")
    alerts.main()
    check("one email sent", len(w.emails), 1)
    body = w.emails[0]["TextBody"]
    checkTrue("quotes the threshold back", "2027-12-31" in body)
    checkTrue("matched date is the earliest qualifying one",
              full["dates"][0] in body)

    # -- 5. threshold exactly on a date -----------------------------
    print("\nthreshold falls exactly on an offered date")
    w = reset(base)
    w.add("e@example.com", full["slug"], full["title"],
          wanted_by=full["dates"][0])
    alerts.main()
    check("on-or-before is inclusive", len(w.emails), 1)

    # -- 6. threshold one day before the earliest -------------------
    print("\nthreshold one day before the earliest date")
    from datetime import date as _d
    day_before = (_d.fromisoformat(full["dates"][0])
                  - timedelta(days=1)).isoformat()
    w = reset(base)
    w.add("f@example.com", full["slug"], full["title"],
          wanted_by=day_before)
    alerts.main()
    check("off by one day means no alert", len(w.emails), 0)

    # -- 7. stale data ----------------------------------------------
    print("\nstale latest.json")
    w = reset(base, age_minutes=200)
    w.add("g@example.com", full["slug"], full["title"])
    alerts.main()
    check("refuses to send from stale data", len(w.emails), 0)
    check("row untouched", w.get(1)["status"], "active")

    # -- 8. unhealthy run -------------------------------------------
    print("\nunhealthy run")
    w = reset(base, healthy=False)
    w.add("h@example.com", full["slug"], full["title"])
    alerts.main()
    check("refuses to send", len(w.emails), 0)

    # -- 9. unknown slug --------------------------------------------
    print("\nsubscription to a document that no longer exists")
    w = reset(base)
    w.add("i@example.com", "some-list-the-court-deleted")
    alerts.main()
    check("no email", len(w.emails), 0)
    check("row left alone for a human to look at",
          w.get(1)["status"], "active")

    # -- 10. unreadable document ------------------------------------
    print("\ndocument could not be read this run")
    broken = json.loads(json.dumps(base))
    for d in broken["documents"]:
        if d["slug"] == full["slug"]:
            d["parse_status"] = "UNREADABLE"
    w = reset(broken)
    w.add("j@example.com", full["slug"], full["title"])
    alerts.main()
    check("unreadable never counts as available", len(w.emails), 0)

    # -- 11. postmark rejects ---------------------------------------
    print("\nPostmark refuses the message")
    w = reset(base)
    w.add("k@example.com", full["slug"], full["title"])
    w.postmark_status = 406
    rc = alerts.main()
    check("row NOT closed on a failed send", w.get(1)["status"], "active")
    check("job reports failure", rc, 1)

    # -- 12. send succeeds but the mark fails -----------------------
    print("\nsent, but the database update fails")
    w = reset(base)
    w.add("l@example.com", full["slug"], full["title"])
    w.patch_fails = True
    rc = alerts.main()
    check("email did go out", len(w.emails), 1)
    check("job reports failure so it is visible", rc, 1)

    # -- 13. already notified ---------------------------------------
    print("\nan already-notified row is never revisited")
    w = reset(base)
    row = w.add("m@example.com", full["slug"], full["title"])
    row["status"] = "notified"
    row["notified_at"] = datetime.now(timezone.utc).isoformat()
    alerts.main()
    check("no second email", len(w.emails), 0)

    # -- 14. confirmation backstop ----------------------------------
    print("\nconfirmation backstop")
    w = reset(base)
    w.add("n@example.com", empty["slug"], empty["title"], confirmed=False)
    alerts.main()
    check("one confirmation sent", len(w.emails), 1)
    checkTrue("confirmation subject",
              "on the list" in w.emails[0]["Subject"])
    checkTrue("says the list is currently empty",
              "no dates on it at all" in w.emails[0]["TextBody"])
    checkTrue("confirmation_sent_at now set",
              w.get(1)["confirmation_sent_at"])
    check("subscription stays open", w.get(1)["status"], "active")

    # -- 15. many subscribers, one list -----------------------------
    print("\nseveral people on the same list")
    w = reset(base)
    for i in range(5):
        w.add("p%d@example.com" % i, full["slug"], full["title"])
    alerts.main()
    check("everyone gets exactly one", len(w.emails), 5)
    check("all rows closed",
          sum(1 for r in w.rows if r["status"] == "notified"), 5)
    alerts.main()
    check("and nothing on a second pass", len(w.emails), 5)

    # -- 16. phone-only hearing types -------------------------------
    print("\nphone-only hearing types must not offer online booking")
    w = reset(base)
    w.add("q@example.com", "bankruptcy-discharge", "Bankruptcy Discharge",
          group="Registrar")
    alerts.main()
    if w.emails:
        body = w.emails[0]["TextBody"]
        check("no online booking link", alerts.BOOK_URL in body, False)
        checkTrue("phone number given", alerts.PHONE in body)
    else:
        check("bankruptcy had dates to alert on", True, False)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
