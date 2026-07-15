"""Scrapes upcoming/current IPO listings (Mainboard + SME) from Chittorgarh.

Chittorgarh publishes plain HTML tables of mainline and SME IPOs. This
module fetches those pages and normalizes each row into an ``IPORecord``.

Because public IPO tracker sites redesign their markup without notice (and
may sit behind anti-bot protection such as Cloudflare), the parser works
generically: it extracts every table on the page and picks the one whose
headers best match the columns we expect, then maps columns by fuzzy
keyword matching rather than a fixed index/selector. If a site blocks the
request outright (403) or changes shape enough that no table matches, the
fetch is logged and skipped for that cycle rather than crashing the bot --
update the URLs/keyword lists in ``config.py`` / below if a source goes
permanently stale.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

import pandas as pd

from config import settings
from database import IPORecord
from utils import (
    FetchError,
    build_column_map,
    clean_text,
    extract_tables,
    fetch_html,
    find_best_table,
    logger,
    now_iso,
    parse_flexible_date,
)

# Keyword groups used to identify the correct table on the page.
_TABLE_KEYWORD_GROUPS: Sequence[Sequence[str]] = (
    ("ipo", "company", "issue"),
    ("open",),
    ("close",),
    ("price", "band"),
    ("lot",),
)

# Keyword lists used to map the winning table's columns to our fields.
_FIELD_KEYWORDS: Dict[str, Sequence[str]] = {
    "company_name": ("ipo name", "company", "issuer", "ipo"),
    "price_band": ("price band", "price", "band"),
    "lot_size": ("lot size", "lot"),
    "issue_size": ("issue size", "size"),
    "open_date": ("open date", "open"),
    "close_date": ("close date", "close"),
    "listing_date": ("listing date", "listing"),
    "allotment_date": ("allotment date", "allotment", "basis of allotment"),
    "registrar": ("registrar",),
    "exchange": ("exchange", "listing at"),
}


class IPOScraper:
    """Fetches and normalizes Mainboard + SME IPO listings."""

    def fetch_all(self) -> List[IPORecord]:
        """Fetch both Mainboard and SME IPOs. Never raises -- logs and
        returns whatever sources succeeded (possibly an empty list)."""
        records: List[IPORecord] = []
        records.extend(self._fetch_segment(settings.chittorgarh_mainboard_url, "Mainboard"))
        records.extend(self._fetch_segment(settings.chittorgarh_sme_url, "SME"))
        logger.info("Scraper collected %d IPO listing(s) this cycle", len(records))
        return records

    def _fetch_segment(self, url: str, ipo_type: str) -> List[IPORecord]:
        try:
            html = fetch_html(url)
        except (FetchError, Exception) as exc:  # noqa: BLE001
            logger.error("Failed to fetch %s IPO list from %s: %s", ipo_type, url, exc)
            return []

        tables = extract_tables(html)
        if not tables:
            logger.warning("No HTML tables found on %s (%s)", url, ipo_type)
            return []

        table = find_best_table(tables, _TABLE_KEYWORD_GROUPS)
        if table is None:
            logger.warning(
                "Could not identify the IPO table on %s (%s) -- site markup may "
                "have changed; update scraper.py keyword groups if this persists.",
                url, ipo_type,
            )
            return []

        column_map = build_column_map(list(table.columns), _FIELD_KEYWORDS)
        if "company_name" not in column_map:
            logger.warning("IPO table on %s has no recognizable name column", url)
            return []

        records: List[IPORecord] = []
        for _, row in table.iterrows():
            record = self._row_to_record(row, column_map, ipo_type, url)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _row_to_record(
        row: "pd.Series", column_map: Dict[str, str], ipo_type: str, source_url: str
    ) -> "IPORecord | None":
        company_name = clean_text(row.get(column_map.get("company_name", ""), ""))
        if not company_name or company_name.lower() in {"nan", "ipo name", ""}:
            return None

        def field(name: str) -> str:
            col = column_map.get(name)
            return clean_text(row.get(col, "")) if col else ""

        return IPORecord(
            ipo_name=company_name,
            company_name=company_name,
            ipo_type=ipo_type,
            price_band=field("price_band"),
            lot_size=field("lot_size"),
            issue_size=field("issue_size"),
            open_date=parse_flexible_date(field("open_date")),
            close_date=parse_flexible_date(field("close_date")),
            listing_date=parse_flexible_date(field("listing_date")),
            allotment_date=parse_flexible_date(field("allotment_date")),
            registrar=field("registrar"),
            exchange=field("exchange") or ("NSE SME/BSE SME" if ipo_type == "SME" else "NSE/BSE"),
            source_url=source_url,
            last_updated=now_iso(),
        )
