"""Scrapes Grey Market Premium (GMP) data for active IPOs.

Tries InvestorGain first, then falls back to IPO Watch if the primary
source is unavailable or its table can't be identified. Both publish a
plain HTML table of "IPO name -> GMP / Kostak / Subject to Sauda / Est.
Listing price" which is parsed generically (see the note in scraper.py
about why fixed selectors are avoided).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from config import settings
from utils import (
    FetchError,
    build_column_map,
    clean_text,
    extract_tables,
    fetch_html,
    find_best_table,
    logger,
    safe_float,
)

_TABLE_KEYWORD_GROUPS: Sequence[Sequence[str]] = (
    ("ipo", "company"),
    ("gmp", "premium"),
)

_FIELD_KEYWORDS: Dict[str, Sequence[str]] = {
    "company_name": ("ipo name", "company", "ipo"),
    "gmp": ("gmp", "premium"),
    "kostak": ("kostak",),
    "subject_to_sauda": ("subject to sauda", "sauda"),
    "estimated_listing_price": ("est listing", "estimated listing", "listing price"),
    "price": ("price",),
}


@dataclass
class GMPQuote:
    """A single GMP reading for one IPO, as scraped from a source."""

    company_name: str
    gmp: Optional[float]
    kostak: str = ""
    subject_to_sauda: str = ""
    source_url: str = ""


class GMPScraper:
    """Fetches the latest GMP table, trying sources in priority order."""

    def fetch_all(self) -> List[GMPQuote]:
        for url in (settings.investorgain_gmp_url, settings.ipowatch_gmp_url):
            quotes = self._fetch_source(url)
            if quotes:
                return quotes
            logger.warning("GMP source %s returned no data, trying next source", url)
        logger.error("All GMP sources failed this cycle -- no GMP data collected")
        return []

    def _fetch_source(self, url: str) -> List[GMPQuote]:
        try:
            html = fetch_html(url)
        except (FetchError, Exception) as exc:  # noqa: BLE001
            logger.error("Failed to fetch GMP data from %s: %s", url, exc)
            return []

        tables = extract_tables(html)
        if not tables:
            logger.warning("No HTML tables found on GMP source %s", url)
            return []

        table = find_best_table(tables, _TABLE_KEYWORD_GROUPS)
        if table is None:
            logger.warning(
                "Could not identify the GMP table on %s -- site markup may have "
                "changed; update gmp.py keyword groups if this persists.", url,
            )
            return []

        column_map = build_column_map(list(table.columns), _FIELD_KEYWORDS)
        if "company_name" not in column_map or "gmp" not in column_map:
            logger.warning("GMP table on %s is missing required columns", url)
            return []

        quotes: List[GMPQuote] = []
        for _, row in table.iterrows():
            name = clean_text(row.get(column_map["company_name"], ""))
            if not name or name.lower() in {"nan", "ipo name", ""}:
                continue
            quotes.append(
                GMPQuote(
                    company_name=name,
                    gmp=safe_float(row.get(column_map["gmp"])),
                    kostak=clean_text(row.get(column_map.get("kostak", ""), "")),
                    subject_to_sauda=clean_text(
                        row.get(column_map.get("subject_to_sauda", ""), "")
                    ),
                    source_url=url,
                )
            )
        logger.info("Fetched %d GMP quote(s) from %s", len(quotes), url)
        return quotes

    @staticmethod
    def match_quote(company_name: str, quotes: List[GMPQuote]) -> Optional[GMPQuote]:
        """Fuzzy-match an IPO name against scraped GMP quotes.

        GMP sources and the IPO listing source often spell company names
        slightly differently (e.g. suffixes like 'Limited' vs 'Ltd').
        Matching is done on a normalized, lowercased, alnum-only form with
        containment in either direction.
        """
        target = _normalize_name(company_name)
        best: Optional[GMPQuote] = None
        best_len = 0
        for quote in quotes:
            candidate = _normalize_name(quote.company_name)
            if candidate == target:
                return quote
            if candidate in target or target in candidate:
                if len(candidate) > best_len:
                    best = quote
                    best_len = len(candidate)
        return best


def _normalize_name(name: str) -> str:
    import re

    text = name.lower()
    for suffix in ("limited", "ltd.", "ltd", "ipo", "&"):
        text = text.replace(suffix, "")
    return re.sub(r"[^a-z0-9]", "", text)
