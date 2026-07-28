#!/usr/bin/env python3
"""
Minimal PDF writer for test fixtures.

Exists so the test suite has no dependency beyond what the scraper
itself needs. A suite that requires an extra install is a suite people
skip, and it would have to be installed on the CI runner too.

Produces uncompressed, single-font PDFs that pdfplumber reads cleanly.
Not a general-purpose writer: enough for fixtures, nothing more.
"""

LINES_PER_PAGE = 52
LINE_HEIGHT = 14
TOP = 780
LEFT = 60
FONT_SIZE = 10


def _escape(s):
    return (s.replace("\\", r"\\")
             .replace("(", r"\(")
             .replace(")", r"\)"))


def _content_stream(lines):
    parts = ["BT", "/F1 %d Tf" % FONT_SIZE, "%d %d Td" % (LEFT, TOP),
             "%d TL" % LINE_HEIGHT]
    for i, line in enumerate(lines):
        parts.append("(%s) Tj" % _escape(line))
        if i != len(lines) - 1:
            parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1", "replace")


def make_pdf(lines, producer=None):
    """
    Build a PDF from a list of text lines, paginating automatically.

    `producer` changes the file's bytes without changing its text, which
    is how the re-export case is simulated.
    """
    pages = [lines[i:i + LINES_PER_PAGE]
             for i in range(0, max(len(lines), 1), LINES_PER_PAGE)] or [[]]

    objects = []          # list of byte strings, 1-indexed on output
    n_pages = len(pages)

    # 1 catalog, 2 pages tree, then per page: page obj + content obj,
    # then the font.
    page_ids = [3 + (i * 2) for i in range(n_pages)]
    content_ids = [4 + (i * 2) for i in range(n_pages)]
    font_id = 3 + (n_pages * 2)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join("%d 0 R" % p for p in page_ids)
    objects.append(("<< /Type /Pages /Kids [%s] /Count %d >>"
                    % (kids, n_pages)).encode())

    for i, page_lines in enumerate(pages):
        objects.append((
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Contents %d 0 R /Resources << /Font << /F1 %d 0 R >> >> >>"
            % (content_ids[i], font_id)).encode())
        stream = _content_stream(page_lines)
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream"
                       % (len(stream), stream))

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    if producer:
        objects.append(("<< /Producer (%s) >>" % _escape(producer)).encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i
        out += body
        out += b"\nendobj\n"

    xref_at = len(out)
    count = len(objects) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off

    trailer = "<< /Size %d /Root 1 0 R" % count
    if producer:
        trailer += " /Info %d 0 R" % len(objects)
    trailer += " >>"
    out += b"trailer\n" + trailer.encode()
    out += b"\nstartxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


def make_bcsc_pdf(category, iso_dates, created,
                  preamble="Current available dates for Vancouver are "
                           "listed below.",
                  location="Vancouver"):
    """
    A PDF in the court's own envelope format.

    Dates go in as ISO and come out in the court's format A,
    'Monday, 22 Mar 2027'.
    """
    from datetime import date as _date

    lines = [
        preamble,
        location,
        "Supreme Court",
        "Available court date(s): %s" % category,
        "Date Created: %s" % created,
    ]
    for iso in iso_dates:
        d = _date.fromisoformat(iso)
        lines.append("%s, %d %s %d" % (d.strftime("%A"), d.day,
                                       d.strftime("%b"), d.year))
    return make_pdf(lines)
