"""
Azure Function: once a day, check every SOURCES entry for articles that are
not recorded yet, and add a record for each new one.

What changed compared with the local script:
  - Wrapped in an Azure timer trigger (replaces daily_scheduler.py).
  - The list of known articles lives in Blob Storage, not in a local file.
    In Azure the code folder is read-only and gets wiped, so a local
    new_articles.json would crash on save and forget everything.
  - Known articles are skipped BEFORE their page is fetched, so a normal
    daily run only opens the listing pages plus whatever is new.
  - Progress is saved after every source, and the run stops itself before
    Azure's 10 minute limit. Anything left over is picked up by the next
    run, so the first big fill can safely take several runs.

Storage: uses the storage account the Function App already has
(AzureWebJobsStorage app setting). Data lands in
container "scraper-data" -> blob "new_articles.json".

Record shape (unchanged):
  date, source, doc_type, title, link, summary, pdf_link, predicted_pdf_link
"""

import datetime
import json
import logging
import os
import re
import time

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient

from mfsa_pdf_scraper import SOURCES, DocumentLink, scrape_source
from predict_pdf_link import predict_pdf_link

CONTAINER = "scraper-data"
BLOB_NAME = "new_articles.json"

# host.json sets the Azure limit to 10 minutes. Stop well before that.
SCRAPE_BUDGET_SECONDS = 6.5 * 60  # no new page fetches after this
TOTAL_BUDGET_SECONDS = 8.5 * 60   # no new records after this

app = func.FunctionApp()


def _snake_case(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip())
    return re.sub(r"_+", "_", text).strip("_").lower()


def _get_blob():
    conn = os.environ.get("AzureWebJobsStorage")
    if not conn or "AccountName=" not in conn:
        raise RuntimeError(
            "AzureWebJobsStorage is missing or is not a connection string. "
            "Check Settings > Environment variables on the Function App."
        )
    container = BlobServiceClient.from_connection_string(conn).get_container_client(CONTAINER)
    if not container.exists():
        container.create_container()
    return container.get_blob_client(BLOB_NAME)


def _load_records(blob) -> list[dict]:
    try:
        return json.loads(blob.download_blob().readall())
    except ResourceNotFoundError:
        return []


def _save_records(blob, records: list[dict]) -> None:
    blob.upload_blob(json.dumps(records, indent=2, ensure_ascii=False), overwrite=True)


def _to_record(doc: DocumentLink) -> dict:
    predicted = None
    if doc.article_title and doc.date:
        try:
            date_obj = datetime.date.fromisoformat(doc.date)
            predicted = predict_pdf_link(doc.doc_type, doc.article_title, date_obj)
        except Exception as exc:
            logging.warning("couldn't predict pdf link for %s: %s", doc.article_url, exc)

    return {
        "date": doc.date,
        "source": doc.source,
        "doc_type": _snake_case(doc.doc_type),
        "title": doc.article_title,
        "link": doc.article_url,
        "summary": doc.summary or doc.article_title,
        "pdf_link": doc.pdf_url,
        "predicted_pdf_link": predicted,
    }


def check_for_new_articles(sources: list[dict] = SOURCES) -> list[dict]:
    """Scrape `sources`, save any article not recorded yet. Returns the new records."""
    start = time.monotonic()
    scrape_deadline = start + SCRAPE_BUDGET_SECONDS
    hard_deadline = start + TOTAL_BUDGET_SECONDS

    blob = _get_blob()
    records = _load_records(blob)
    seen = {r["link"] for r in records}
    all_new: list[dict] = []

    for i, source in enumerate(sources):
        if time.monotonic() > scrape_deadline:
            logging.warning("time budget used up, the remaining sources wait for the next run")
            break
        if i > 0:
            time.sleep(5)  # spread load to stay under bot-protection rate limits

        try:
            docs = scrape_source(source, skip_urls=seen, deadline=scrape_deadline)
        except Exception as exc:
            logging.warning("failed on %s: %s", source["url"], exc)
            continue

        new_here = []
        for doc in docs:  # oldest first
            if doc.article_url in seen:
                continue
            if time.monotonic() > hard_deadline:
                break
            new_here.append(_to_record(doc))
            seen.add(doc.article_url)

        if new_here:
            records.extend(new_here)
            all_new.extend(new_here)
            _save_records(blob, records)  # save per source so a cut-off run keeps its work
        logging.info("%s: %d new", source["doc_type"], len(new_here))

    return all_new


# NCRONTAB is in UTC: 11:00 UTC = 13:00 in Malta in summer, 12:00 in winter.
@app.timer_trigger(schedule="0 0 11 * * *", arg_name="timer", run_on_startup=False)
def check_new_articles(timer: func.TimerRequest) -> None:
    new = check_for_new_articles()
    logging.info("done - %d new article(s)", len(new))
    for r in new:
        logging.info("NEW: %s | %s | %s", r["date"], r["title"], r["link"])
