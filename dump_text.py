#!/usr/bin/env python3
"""
Dump the extracted text of every archived Vancouver PDF.

Run after a cold scrape. Writes extracted-text.txt, which is what the
parser will actually see. Useful for eyeballing layout variation before
anyone writes a line of parsing code.

Usage:
    python dump_text.py
    python dump_text.py "3 Day Civil"     # only matching titles
"""

import io
import os
import re
import sys

import pdfplumber

RAW = os.path.join("data", "raw")
OUT = "extracted-text.txt"
PREVIEW_CHARS = 1200


def archived():
    """Every archived file, newest version per title."""
    found = {}
    for root, _dirs, files in os.walk(RAW):
        for fn in files:
            path = os.path.join(root, fn)
            # filenames are <hash16>-<slug>.<ext>
            m = re.match(r"^[0-9a-f]{16}-(.+)\.(pdf|docx?)$", fn)
            slug = m.group(1) if m else fn
            mtime = os.path.getmtime(path)
            if slug not in found or mtime > found[slug][1]:
                found[slug] = (path, mtime)
    return sorted((slug, p) for slug, (p, _) in found.items())


def text_of(path):
    with open(path, "rb") as f:
        data = f.read()
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
        return pages, None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def main():
    filt = sys.argv[1].lower() if len(sys.argv) > 1 else None

    if not os.path.isdir(RAW):
        print("No %s directory. Run scrape.py first." % RAW)
        return 1

    docs = archived()
    if filt:
        docs = [(s, p) for s, p in docs if filt in s.lower()]

    if not docs:
        print("Nothing matched.")
        return 1

    print("%d document(s)" % len(docs))
    print()

    with open(OUT, "w", encoding="utf-8") as out:
        for slug, path in docs:
            pages, err = text_of(path)
            size = os.path.getsize(path)

            header = "=" * 70
            out.write("%s\n%s\n" % (header, slug))
            out.write("file: %s  (%d bytes)\n" % (path, size))

            if err:
                out.write("EXTRACTION FAILED: %s\n\n" % err)
                print("  FAILED   %-45s %s" % (slug, err))
                continue

            joined = "\n".join(pages)
            chars = len(joined.strip())
            out.write("pages: %d, characters: %d\n%s\n\n"
                      % (len(pages), chars, header))
            for i, p in enumerate(pages):
                out.write("--- page %d ---\n%s\n\n" % (i + 1, p))
            out.write("\n")

            flag = "  <- no text" if chars == 0 else ""
            print("  ok       %-45s %d pages, %d chars%s"
                  % (slug, len(pages), chars, flag))

    print()
    print("Written to %s" % OUT)
    print()
    print("Preview of the first document:")
    print("-" * 70)
    pages, err = text_of(docs[0][1])
    if err:
        print(err)
    else:
        print("\n".join(pages)[:PREVIEW_CHARS])
    print("-" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
