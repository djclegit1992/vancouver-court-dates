#!/usr/bin/env python3
"""
Build the Vancouver availability grid.

Consumes parsed documents and produces the payload the page reads.
The grid is matter type down the side, trial length across the top,
with the next available date in each cell.

Every date is YYYY-MM-DD. Every time is 24-hour HH:MM, Pacific.

Cells are one of four states, and they are never conflated:

  available   the court is offering dates
  none        envelope intact, court is offering nothing
  unreadable  we could not read the document
  missing     no document exists for that combination
"""

import json
import os
import re
from datetime import date, datetime

import parse

# Order matters: this is the column order on the page.
LENGTHS = ["2 Days", "3 Days", "4-5 Days", "6-15 Days", "16+ Days"]
MATTERS = ["Civil", "Family", "MVA"]

MATTER_LABEL = {
    "Civil": "Civil",
    "Family": "Family",
    "MVA": "Motor vehicle",
}

LENGTH_LABEL = {
    "2 Days": "2 days",
    "3 Days": "3 days",
    "4-5 Days": "4-5 days",
    "6-15 Days": "6-15 days",
    "16+ Days": "16+ days",
}

AVAILABLE, NONE, UNREADABLE, MISSING = (
    "available", "none", "unreadable", "missing")

# Colour bands, in days. Playbook 6.3.
BANDS = [(14, "soon"), (45, "weeks"), (10 ** 6, "months")]


def band_for(days):
    if days is None:
        return None
    for limit, name in BANDS:
        if days <= limit:
            return name
    return "months"


def cell_from(result, today):
    """One grid cell from one parse result."""
    if result is None:
        return {"state": MISSING}

    if result.status == parse.UNREADABLE:
        return {"state": UNREADABLE, "reason": result.reason}

    if result.status == parse.NO_DATES:
        return {"state": NONE, "reason": result.reason}

    dates = result.dates
    earliest = dates[0]
    wait = (date.fromisoformat(earliest) - today).days

    # A date carrying a qualifier is not usable for the full band.
    qualified = sorted(set(
        s["date"] for s in result.slots if s["note"]))
    unqualified = sorted(set(
        s["date"] for s in result.slots if not s["note"]))

    cell = {
        "state": AVAILABLE,
        "earliest_date": earliest,
        "wait_days": wait,
        "band": band_for(wait),
        "dates_offered": len(dates),
        "last_date": dates[-1],
    }

    if qualified:
        cell["qualified_dates"] = len(qualified)
        cell["notes"] = sorted(set(
            s["note"] for s in result.slots if s["note"]))
        if unqualified:
            first_full = unqualified[0]
            cell["earliest_unqualified_date"] = first_full
            cell["unqualified_wait_days"] = (
                date.fromisoformat(first_full) - today).days
    return cell


def cell_from_info(info, today):
    """
    One grid cell from a cached parse record (a plain dict), rather than
    a live ParseResult. This is the path the scraper uses, so an
    unchanged document never has to be re-parsed to redraw the grid.
    """
    if info is None:
        return {"state": MISSING}

    status = info.get("parse_status")
    if status == "UNREADABLE":
        return {"state": UNREADABLE, "reason": info.get("parse_reason")}
    if status == "NO_DATES":
        return {"state": NONE, "reason": info.get("parse_reason")}

    dates = info.get("dates") or []
    if not dates:
        return {"state": NONE, "reason": info.get("parse_reason")}

    earliest = dates[0]
    wait = (date.fromisoformat(earliest) - today).days
    cell = {
        "state": AVAILABLE,
        "earliest_date": earliest,
        "wait_days": wait,
        "band": band_for(wait),
        "dates_offered": len(dates),
        "last_date": dates[-1],
    }

    qualified = info.get("qualified_dates") or []
    unqualified = info.get("unqualified_dates") or []
    if qualified:
        cell["qualified_dates"] = len(qualified)
        cell["notes"] = info.get("notes") or []
        if unqualified:
            cell["earliest_unqualified_date"] = unqualified[0]
            cell["unqualified_wait_days"] = (
                date.fromisoformat(unqualified[0]) - today).days
    return cell


def build_from_info(info_by_category, today=None, generated_at=None):
    """Same payload as build(), from cached parse dicts."""
    today = today or date.today()
    stubs = {}
    for key, info in info_by_category.items():
        stubs[key] = info
    return _assemble(
        lambda k: cell_from_info(stubs.get(k), today), today, generated_at)


def _assemble(cell_fn, today, generated_at):
    grid = []
    for matter in MATTERS:
        row = {"matter": matter, "label": MATTER_LABEL[matter], "cells": []}
        for length in LENGTHS:
            c = cell_fn((matter, length))
            c["length"] = length
            c["length_label"] = LENGTH_LABEL[length]
            row["cells"].append(c)
        avail = [c["earliest_date"] for c in row["cells"]
                 if c["state"] == AVAILABLE]
        row["earliest_in_row"] = min(avail) if avail else None
        grid.append(row)

    waits = [c["wait_days"] for r in grid for c in r["cells"]
             if c["state"] == AVAILABLE]

    return {
        "as_at": today.isoformat(),
        "generated_at": generated_at or datetime.now().isoformat(
            timespec="seconds"),
        "date_format": "YYYY-MM-DD",
        "timezone": "America/Vancouver",
        "lengths": [{"key": l, "label": LENGTH_LABEL[l]} for l in LENGTHS],
        "grid": grid,
        "summary": {
            "cells_total": len(MATTERS) * len(LENGTHS),
            "cells_available": len(waits),
            "shortest_wait_days": min(waits) if waits else None,
            "longest_wait_days": max(waits) if waits else None,
        },
    }


def build(results_by_category, today=None, generated_at=None):
    """
    results_by_category: {(matter, length): ParseResult}
    """
    today = today or date.today()
    return _assemble(
        lambda k: cell_from(results_by_category.get(k), today),
        today, generated_at)


def categorise(result):
    """Map a parse result onto a grid coordinate, or None."""
    if not result.ok or not result.length or not result.matter:
        return None
    length = result.length.strip()
    matter = result.matter.strip()
    if length in LENGTHS and matter in MATTERS:
        return (matter, length)
    return None


def from_directory(raw_dir, today=None):
    """Parse every archived PDF and build the grid."""
    results = {}
    others = []
    for root, _d, files in os.walk(raw_dir):
        for fn in sorted(files):
            if not fn.lower().endswith(".pdf"):
                continue
            path = os.path.join(root, fn)
            r = parse.parse_file(path)
            key = categorise(r)
            if key:
                results[key] = r
            else:
                others.append((fn, r))
    return build(results, today=today), results, others


if __name__ == "__main__":
    import sys
    raw = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "raw")
    today = (date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2
             else date.today())
    payload, _res, _other = from_directory(raw, today=today)
    print(json.dumps(payload, indent=2))
