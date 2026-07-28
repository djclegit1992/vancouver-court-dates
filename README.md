# Vancouver Court Dates Finder

Monitors the availability lists the Supreme Court of British Columbia
publishes for its Vancouver registry, and makes them readable in one
place.

Built by [Courtready](https://courtready.ca). The repository is public
so the methodology can be inspected rather than taken on trust.

## What this is

The court publishes 31 documents for Vancouver on a single page. To
find out when you could get a three-day civil trial, you open one PDF.
Nothing compares them, and nothing tells you when any of them last
changed.

This reads all 31 five times a day, extracts the dates, and assembles
them into a grid: matter type down the side, trial length across the
top, next available date in each cell.

It is **not** a booking system, it holds no private data, and it reads
only what any member of the public can read. Its only claim is that it
reads more often than a person can.

## What it is not

Not live. Five checks a day is not real time, and the page says so.

Not legal advice, and not a substitute for calling Supreme Court
Scheduling on 604.660.2853.

Not the court's own service. The authoritative source is always
[the court's scheduling page](https://www.bccourts.ca/supreme_court/scheduling/index.aspx).

## The rule everything follows

**A confident wrong answer is worse than an admitted gap.**

That is why a failed fetch is never rendered as "no dates available",
why a document we could not parse says so rather than showing zero, and
why a run that looks broken is quarantined rather than published.

Four states are kept distinct and never collapsed:

| State | Meaning |
|---|---|
| dates listed | the court is offering these dates |
| none offered | document read successfully, court is offering nothing |
| could not read | document fetched but not parseable |
| could not check | fetch failed |

## How it works

`scrape.py` reads the court's index page once, collects the Vancouver
links verbatim rather than constructing any URL, and sends a conditional
request per document using the stored `ETag`. Unchanged documents return
`304` with no body.

Each document carries two hashes. `bytes_hash` answers "was the file
replaced". `text_hash` answers "did what it says change". PDFs exported
from Word get a fresh creation date on every save, so bytes alone would
report a change every night whether or not anything moved.

`parse.py` reads the dates. The documents share a common envelope, and
that envelope is what distinguishes an empty list from an unreadable
one. It also validates that the body names Vancouver, so a wrong
document served with a `200` is rejected rather than published.

`grid.py` assembles the availability grid.

All dates are `YYYY-MM-DD`. All times are 24-hour and Pacific, the
court's own timezone, regardless of where you are reading from.

## Running it

```
pip install -r requirements.txt
python preflight.py          # three requests, writes nothing
python scrape.py
```

Tests need no network and no extra dependencies:

```
python test_scrape.py            # watcher logic
python test_parse.py data/raw    # parser, against real documents
python test_integration.py data/raw
```

## Data

```
data/latest.json    current state and the grid, read by the website
data/state.json     durable per-document state
data/history.csv    one row per document per run
data/runs/          every run, compressed, kept
data/raw/           one archived copy per distinct document version
data/parsed/        parse cache, keyed by content hash
data/catalogue.json known documents, for detecting additions
data/health.json    consecutive failures per document
```

Archives are keyed on content, not bytes, so a document re-exported
without substantive edits does not create a second copy.

## Being a good citizen

One request every two seconds. An identifying User-Agent with a contact
address. Conditional requests, so a quiet day transfers a few kilobytes.
A skip guard so the backup trigger does not duplicate the primary.

## Contact

admin [at] courtready.ca
