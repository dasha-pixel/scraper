"""
Daily job: check every SOURCES entry for articles not already recorded in
OUTPUT_PATH, and append a record for each new one.

Reuses:
  - mfsa_pdf_scraper.scrape_all_sources() / SOURCES to list every current
    article per source, along with whatever pdf_url, date and summary it
    can pull straight off the article page.
  - predict_pdf_link.predict_pdf_link() to independently predict (and live-
    verify against mfsa.mt) the PDF URL from the title + date, so the
    scraped pdf_url and the predicted one can be cross-checked - useful
    since the scrape can occasionally miss the resource block (rate limit,
    template change) even when the file demonstrably exists.

"New" means article_url isn't already present in OUTPUT_PATH; each run's
output IS the seen-set, there's no separate state file.

Output record shape (one per new article), matching the fields requested:
  date, source, doc_type, title, link, summary, pdf_link, predicted_pdf_link

doc_type is snake_cased from the SOURCES entry (e.g. "Dear CEO letter" ->
"dear_ceo_letter"); source is copied as-is from the SOURCES entry.

Scheduling: see daily_scheduler.py in this directory - a long-running
Python loop (not OS crontab/launchd) that calls check_for_new_articles()
once a day at a fresh random time between 12:00 and 16:00.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

from mfsa_pdf_scraper import SOURCES, DocumentLink, scrape_all_sources
from predict_pdf_link import predict_pdf_link

OUTPUT_PATH = Path(__file__).parent / "new_articles.json"


def _snake_case(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip())
    return re.sub(r"_+", "_", text).strip("_").lower()


def _load_seen_links(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        records = json.load(f)
    return {r["link"] for r in records}


def _to_record(doc: DocumentLink) -> dict:
    predicted = None
    if doc.article_title and doc.date:
        try:
            date_obj = datetime.date.fromisoformat(doc.date)
            predicted = predict_pdf_link(doc.doc_type, doc.article_title, date_obj)
        except Exception as exc:
            print(f"[warn] couldn't predict pdf link for {doc.article_url}: {exc}", file=sys.stderr)

    return {
        "date": doc.date,
        "source": doc.source,
        "doc_type": _snake_case(doc.doc_type),
        "title": doc.article_title,
        "link": doc.article_url,
        "summary": doc.summary or doc.article_title,  # fall back when the page has no meta description
        "pdf_link": doc.pdf_url,
        "predicted_pdf_link": predicted,
    }


def check_for_new_articles(sources: list[dict] = SOURCES, output_path: Path = OUTPUT_PATH) -> list[dict]:
    """Scrape `sources`, append any article not already in `output_path` to it.
    Returns just the newly-added records."""
    seen_links = _load_seen_links(output_path)
    existing_records = []
    if output_path.exists():
        with output_path.open(encoding="utf-8") as f:
            existing_records = json.load(f)

    docs = scrape_all_sources(sources)
    new_records = [
        _to_record(doc) for doc in docs if doc.article_url not in seen_links
    ]

    if new_records:
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(existing_records + new_records, f, indent=2, ensure_ascii=False)

    return new_records


if __name__ == "__main__":
    new = check_for_new_articles()
    if new:
        print(f"{len(new)} new article(s) found and appended to {OUTPUT_PATH}:")
        print(json.dumps(new, indent=2, ensure_ascii=False))
    else:
        print("No new articles found.")
