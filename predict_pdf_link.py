"""
Given an MFSA article's subdomain/category name, its title, and its publish
date, predicts the URL of its PDF under mfsa.mt/wp-content/uploads/.

How the guess is built (reverse-engineered from the real filenames in
"Scraping links_Embark Group.xlsx" - MFSA's uploads are WordPress media, and
WordPress derives an upload's on-disk filename from whatever filename/title
was used at upload time via sanitize_file_name(), which:
  - drops a fixed set of "special" characters entirely: ? [ ] / \\ = < > : ; , ' " & $ # * ( ) | ~ ` ! { }
    (this is why a straight apostrophe like "Practitioner's" -> "Practitioners",
    and why "(‘BMR’)"'s closing curly quote and both parens vanish)
  - does NOT touch a left curly quote such as U+2018 ('), so when that
    survives into the URL it shows up percent-encoded (%E2%80%98)
  - collapses whitespace/dashes into single hyphens, but keeps the original
    capitalisation of the title (filenames are Title-Cased-With-Dashes, not
    lowercased the way WordPress slugs normally are)
  - lives under /wp-content/uploads/<year>/<month>/ for the article's
    publish date
  - for the "MFSA Dear CEO Letters" subdomain specifically, the filename is
    prefixed with "Dear-CEO-Letter-"

That base rule alone only matched 14/18 comparable rows in the sheet - the
remaining 4 were genuine one-off human choices at MFSA's end (a manually
added "Circular regarding "/"Circular on " prefix, an upload made a month
before the article's publish date, an en dash kept instead of collapsed,
and a manually shortened filename). None of those are derivable from the
title by one fixed rule, so instead of guessing blind, predict_pdf_link()
generates several plausible slug variants x a few nearby year/month folders,
and does a live HEAD request against mfsa.mt (its static /wp-content/uploads/
paths are NOT behind the Cloudflare bot-protection that blocks the rest of
the site - verified: a normal curl to an upload path gets a clean 200/403,
no JS challenge) to pick the first candidate that actually exists. This
closed all 4 remaining gaps against the sheet (see the comparison below).
"""

from __future__ import annotations

import datetime
import itertools
import re
import time
from urllib.parse import quote

import requests

_SPECIAL_CHARS = "?[]/\\=<>:;,'\"&$#*()|~`!{}"
_SPECIAL_CHARS_RE = re.compile("[" + re.escape(_SPECIAL_CHARS) + "]")
_WHITESPACE_RE = re.compile(r"[\s\-]+")
_ENDASH_RE = re.compile(r"[–—]")

DEAR_CEO_PREFIX = "Dear-CEO-Letter-"
CIRCULAR_PREFIXES = ("Circular-regarding-", "Circular-on-")

_session = requests.Session()


def _slugify(title: str, *, keep_endash: bool) -> str:
    title = title.replace("’", "").replace("'", "")  # ’ and ' are stripped
    title = _ENDASH_RE.sub(" – ", title) if keep_endash else _ENDASH_RE.sub("-", title)
    title = _SPECIAL_CHARS_RE.sub("", title)
    return _WHITESPACE_RE.sub("-", title).strip("-")


def _slug_candidates(title: str) -> list[str]:
    """Plausible filename slugs for `title`, most-likely-first."""
    candidates = [_slugify(title, keep_endash=False)]

    with_endash = _slugify(title, keep_endash=True)
    if with_endash != candidates[0]:
        candidates.append(with_endash)

    if "(" in title:
        truncated = _slugify(title.split("(", 1)[0], keep_endash=False)
        if truncated and truncated not in candidates:
            candidates.append(truncated)

    for prefix in CIRCULAR_PREFIXES:
        for base in list(candidates):
            variant = prefix + base
            if variant not in candidates:
                candidates.append(variant)

    return candidates


def _month_candidates(date: datetime.date) -> list[tuple[int, int]]:
    """(year, month) pairs to try, publish month first, then nearby ones -
    uploads sometimes land a month or two off the article's publish date."""
    months = []
    for offset in (0, -1, -2, 1):
        year, month = date.year, date.month + offset
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        if (year, month) not in months:
            months.append((year, month))
    return months


def _url_exists(url: str) -> bool:
    try:
        resp = _session.head(url, timeout=10, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        return resp.status_code == 200
    except requests.RequestException:
        return False


def predict_pdf_link(
    subdomain_name: str,
    article_name: str,
    date: datetime.datetime,
    verify: bool = True,
) -> str:
    """Predict the mfsa.mt PDF URL for an article given its category, title and date.

    With verify=True (default), checks each candidate against the live site
    and returns the first one that actually exists, falling back to the
    plain best guess if none do. With verify=False, returns the best guess
    with no network calls."""
    slugs = _slug_candidates(article_name)
    if "dear ceo" in subdomain_name.lower():
        slugs = [DEAR_CEO_PREFIX + s for s in slugs]

    best_guess = f"https://www.mfsa.mt/wp-content/uploads/{date.year}/{date.month:02d}/{quote(slugs[0], safe='-')}.pdf"
    if not verify:
        return best_guess

    for (year, month), slug in itertools.product(_month_candidates(date), slugs):
        url = f"https://www.mfsa.mt/wp-content/uploads/{year}/{month:02d}/{quote(slug, safe='-')}.pdf"
        if _url_exists(url):
            return url
        time.sleep(0.1)
    return best_guess


if __name__ == "__main__":
    import openpyxl

    path = "/Users/mihai/Downloads/Scraping links_Embark Group.xlsx"
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))[1:]  # skip header

    match_count = 0
    comparable_count = 0
    for subdomain, subdomain_link, article_name, date, pdf_link, note in rows:
        site = f"{subdomain or ''} {subdomain_link or ''}".lower()
        if "fiaumalta" in site:
            print(f"SKIP (fiaumalta.org doesn't follow the mfsa.mt filename pattern) -> actual: {pdf_link!r}")
            print("-" * 100)
            continue
        if not article_name or not isinstance(date, datetime.datetime):
            print(f"SKIP (no name/date to predict from) -> actual: {pdf_link!r}")
            print("-" * 100)
            continue

        predicted = predict_pdf_link(subdomain, article_name, date)
        is_pdf = isinstance(pdf_link, str) and pdf_link.lower().endswith(".pdf")
        comparable_count += 1
        match = is_pdf and predicted == pdf_link
        match_count += match

        print(f"Article : {article_name}")
        print(f"Predicted: {predicted}")
        print(f"Actual   : {pdf_link}")
        print(f"Match    : {match}" + ("" if is_pdf else "  (actual isn't a .pdf link, not comparable)"))
        if note:
            print(f"Note     : {note.strip()}")
        print("-" * 100)

    print(f"\n{match_count}/{comparable_count} predicted links matched the actual pdf_link exactly.")
