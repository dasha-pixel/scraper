"""
Scrapes MFSA (and EUR-Lex) publication pages via the r.jina.ai reader proxy
and extracts, for every article found, its title, article URL and PDF URL.

Why r.jina.ai: mfsa.mt sits behind Cloudflare bot-protection (a plain
requests/curl call gets an HTTP 403 "Attention Required" challenge page).
r.jina.ai fetches the page server-side with its own browser and hands back
the rendered page, which gets past that block.

Why "X-Respond-With: html" instead of jina's default markdown mode: the
default markdown mode runs the page through a Readability-style content
extractor, which on MFSA article pages keeps a *sidebar* list also labelled
"RESOURCES" but drops the real download block (div.single-publication-resources)
because it looks like boilerplate to the extractor. Fetching raw HTML and
parsing it ourselves with BeautifulSoup gets the actual PDF link reliably
(verified by diffing both outputs on a live article).

Site structure this relies on (verified against the live pages):
  - Listing pages (e.g. .../dear-ceo-letters/, .../crypto-assets-circulars/)
    contain article links matching https://www.mfsa.mt/publication/<slug>/,
    optionally paginated via a "Next" link / .../page/N/.
  - Every article page uses the same "single-publication" template; the
    actual PDF(s) live inside div.single-publication-resources a[href$=".pdf"].
  - Some source URLs (e.g. /publications/fiau) redirect straight to a single
    article rather than a listing page.
  - The MiCA source is a EUR-Lex legal act page, not an MFSA listing, and is
    blocked by its own bot-protection (AWS WAF) even through r.jina.ai, so
    its PDF link is derived directly from the CELEX number in the URL
    instead of being scraped.

pip install requests beautifulsoup4
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

JINA_PREFIX = "https://r.jina.ai/"
ARTICLE_URL_RE = re.compile(r"https://www\.mfsa\.mt/publication/[^/\s\"']+/?$")


@dataclass
class DocumentLink:
    source: str
    doc_type: str
    article_title: str | None
    article_url: str
    pdf_url: str | None
    date: str | None = None  # ISO yyyy-mm-dd, from div.created-date on the article page
    summary: str | None = None  # one-sentence summary, from the page's meta description


def _strip_jina_prefix(url: str) -> str:
    return url[len(JINA_PREFIX):] if url.startswith(JINA_PREFIX) else url


_CHALLENGE_MARKERS = ("just a moment", "attention required", "verify you are human")


def _is_challenge_page(html: str) -> bool:
    title = (_extract_title(html) or "").lower()
    return any(marker in title for marker in _CHALLENGE_MARKERS)


def _fetch_html(url: str, retries: int = 5, timeout: int = 60, delay: float = 1.5) -> tuple[str, str]:
    """Fetch `url` (an mfsa.mt page) through the jina reader proxy as raw HTML.
    Returns (final_resolved_url, html). Retries with backoff if the proxy
    itself gets served a Cloudflare/WAF challenge page instead of real content."""
    target = url if url.startswith(JINA_PREFIX) else JINA_PREFIX + url
    headers = {"X-Respond-With": "html"}
    last_exc = None
    for attempt in range(retries):
        time.sleep(delay)
        try:
            resp = requests.get(target, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if _is_challenge_page(resp.text):
                time.sleep(3 * (attempt + 1))
                continue
            final_url = resp.headers.get("X-Final-Url") or _strip_jina_prefix(url)
            return final_url, resp.text
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"gave up after {retries} attempts, still served a challenge page: {url}")


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if not m:
        return None
    title = m.group(1).strip()
    return re.sub(r"\s*-\s*MFSA\s*$", "", title).strip()


def _extract_canonical_url(html: str) -> str | None:
    """<link rel="canonical"> / og:url - more reliable than jina's X-Final-Url
    header for detecting a server-side redirect (seen on /publications/fiau:
    the header still reported the pre-redirect URL, but canonical didn't)."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("link[rel='canonical']")
    if link and link.get("href"):
        return link["href"]
    meta = soup.select_one("meta[property='og:url']")
    if meta and meta.get("content"):
        return meta["content"]
    return None


def _extract_article_pdfs(html: str, article_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    block = soup.select_one("div.single-publication-resources")
    if not block:
        return []
    return [
        urljoin(article_url, a["href"])
        for a in block.select("a[href]")
        if a["href"].lower().endswith(".pdf")
    ]


def _extract_date(html: str) -> str | None:
    """div.created-date holds e.g. 'SEPTEMBER 03, 2026' -> '2026-09-03'."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one("div.created-date")
    if not el:
        return None
    text = " ".join(el.get_text().split())
    try:
        return datetime.datetime.strptime(text.title(), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _extract_summary(html: str) -> str | None:
    """The page's Yoast meta description is already a one-sentence summary."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one("meta[name='description']")
    if not el:
        return None
    content = (el.get("content") or "").strip()
    return content or None


def _extract_article_links(html: str, listing_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    seen = {}
    for a in soup.select("a[href*='/publication/']"):
        href = urljoin(listing_url, a["href"])
        if ARTICLE_URL_RE.match(href) and href not in seen:
            seen[href] = a.get_text(strip=True)
    return list(seen.items())


def _extract_next_page(html: str, listing_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        text = a.get_text(strip=True).lower()
        rel = (a.get("rel") or [""])[0].lower()
        if text == "next" or rel == "next":
            return urljoin(listing_url, a["href"])
    return None


def _eurlex_pdf_url(url: str) -> str:
    """https://eur-lex.europa.eu/eli/reg/2023/1114/oj -> its PDF rendition."""
    m = re.search(r"/eli/reg/(\d{4})/(\d+)/oj", url)
    if m:
        year, num = m.groups()
        celex = f"3{year}R{int(num):04d}"
        return f"https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:{celex}"
    return url


def _past(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def scrape_source(
    source: dict,
    max_pages: int = 5,
    skip_urls: set[str] | None = None,
    deadline: float | None = None,
) -> list[DocumentLink]:
    """Scrape one SOURCES entry and return every article's title/URL/PDF-URL.

    skip_urls: article URLs already recorded - their pages are NOT fetched
    again, and paging stops at the first listing page where everything is
    already known (listings are newest-first, so older pages are known too).
    deadline: a time.monotonic() value; once passed, stop fetching and
    return what we have. Articles are fetched OLDEST first, so whatever got
    skipped is always the newest part and the next run picks it up."""
    skip = skip_urls or set()
    url = _strip_jina_prefix(source["url"])

    if "eur-lex.europa.eu" in url:
        return [
            DocumentLink(
                source=source["source"],
                doc_type=source["doc_type"],
                article_title=None,
                article_url=url,
                pdf_url=_eurlex_pdf_url(url),
            )
        ]

    final_url, html = _fetch_html(url)
    canonical_url = _extract_canonical_url(html)
    if canonical_url and ARTICLE_URL_RE.match(canonical_url):
        final_url = canonical_url

    # Some source URLs (e.g. /publications/fiau) redirect straight to a
    # single article instead of a listing page.
    if ARTICLE_URL_RE.match(final_url):
        pdfs = _extract_article_pdfs(html, final_url)
        return [
            DocumentLink(
                source=source["source"],
                doc_type=source["doc_type"],
                article_title=_extract_title(html),
                article_url=final_url,
                pdf_url=pdfs[0] if pdfs else None,
                date=_extract_date(html),
                summary=_extract_summary(html),
            )
        ]

    articles: dict[str, str] = {}
    page_url = final_url
    for _ in range(max_pages):
        page_links = _extract_article_links(html, page_url)
        for href, text in page_links:
            articles.setdefault(href, text)
        if page_links and all(href in skip for href, _ in page_links):
            break  # whole page already recorded - older pages are too
        if _past(deadline):
            break
        next_url = _extract_next_page(html, page_url)
        if not next_url or next_url == page_url:
            break
        page_url = next_url
        _, html = _fetch_html(page_url)

    results = []
    for article_url, listing_text in reversed(list(articles.items())):  # oldest first
        if article_url in skip:
            continue
        if _past(deadline):
            break
        try:
            _, article_html = _fetch_html(article_url)
        except (requests.RequestException, RuntimeError) as exc:
            print(f"[warn] failed to fetch {article_url}: {exc}", file=sys.stderr)
            results.append(
                DocumentLink(
                    source=source["source"],
                    doc_type=source["doc_type"],
                    article_title=listing_text or None,
                    article_url=article_url,
                    pdf_url=None,
                )
            )
            continue
        pdfs = _extract_article_pdfs(article_html, article_url)
        title = _extract_title(article_html) or listing_text or None
        results.append(
            DocumentLink(
                source=source["source"],
                doc_type=source["doc_type"],
                article_title=title,
                article_url=article_url,
                pdf_url=pdfs[0] if pdfs else None,
                date=_extract_date(article_html),
                summary=_extract_summary(article_html),
            )
        )
    return results


def scrape_all_sources(
    sources: list[dict],
    max_pages: int = 5,
    skip_urls: set[str] | None = None,
    deadline: float | None = None,
) -> list[DocumentLink]:
    all_docs: list[DocumentLink] = []
    for i, source in enumerate(sources):
        if _past(deadline):
            break
        if i > 0:
            time.sleep(5)  # spread load out across sources to avoid bot-protection rate limits
        try:
            all_docs.extend(scrape_source(source, max_pages=max_pages, skip_urls=skip_urls, deadline=deadline))
        except Exception as exc:
            print(f"[warn] failed on {source['url']}: {exc}", file=sys.stderr)
    return all_docs


SOURCES = [
        {
            "source": "MFSA",
            "doc_type": "Dear CEO letter",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/corporate-publications/dear-ceo-letters/",
            "parser": "parse_mfsa_dear_ceo",
        },
        {
            "source": "MFSA",
            "doc_type": "Rulebook / circular change",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/circulars/",
            "parser": "parse_mfsa_circulars",
        },
        {
            "source": "MFSA",
            "doc_type": "Crypto Assets",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/circulars/crypto-assets-circulars/",
            "parser": "parse_mfsa_crypto_assets",
        },
        {
            "source": "MFSA",
            "doc_type": "Financial Institutions",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/circulars/financial-institutions-circulars/",
            "parser": "parse_mfsa_financial_institutions",
        },
        {
            "source": "MFSA",
            "doc_type": "Cybersecurity",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/circulars/supervisory-ict-risk-and-cybersecurity-circulars/",
            "parser": "parse_mfsa_cybersecurity",
        },
        {
            "source": "MFSA",
            "doc_type": "Fintech",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/circulars/fintech/",
            "parser": "parse_mfsa_fintech",
        },
        {
            "source": "MFSA",
            "doc_type": "Investment Firms",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/circulars/investment-services-supervision/",
            "parser": "parse_mfsa_investment_firms",
        },
        {
            "source": "MFSA",
            "doc_type": "AML",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/circulars/anti-money-laundering/",
            "parser": "parse_mfsa_aml",
        },
        {
            "source": "MFSA",
            "doc_type": "FIAU",
            "url": "https://r.jina.ai/https://www.mfsa.mt/publications/fiau",
            "parser": "parse_mfsa_fiau",
        },
        {
            "source": "MFSA",
            "doc_type": "MiCA Regulation",
            "url": "https://r.jina.ai/https://eur-lex.europa.eu/eli/reg/2023/1114/oj",
            "parser": "parse_mfsa_mica",
        },
]


if __name__ == "__main__":
    docs = scrape_all_sources(SOURCES)
    print(json.dumps([asdict(d) for d in docs], indent=2, ensure_ascii=False))
