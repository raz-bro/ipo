"""Scrapes upcoming/current IPO listings (Mainboard + SME).

Primary source: Groww's public IPO page (groww.in/ipo), which -- unlike
Chittorgarh/InvestorGain -- is reachable by plain scripted HTTP requests
(no Cloudflare/WAF block observed). It splits IPOs across three separate
tables on one page (Open / Upcoming / Closed), each with slightly
different columns, so every matching table is parsed and combined rather
than picking just one "best" table.

Chittorgarh is also still attempted as a secondary source (its dedicated
mainline/SME report pages carry a couple of fields Groww doesn't show, like
lot size and registrar) -- if it's blocked (as observed at build time, HTTP
403) it's simply skipped and logged, same as any other source failure.

Because public IPO tracker sites redesign their markup (or their bot
protection) without notice, the parser works generically: it extracts
every table on a page and matches columns by fuzzy keyword rather than a
fixed index/selector. If a source changes shape enough that nothing
matches, the fetch is logged and skipped for that cycle rather than
crashing the bot -- update the URLs/keyword lists in ``config.py`` / below
if a source goes permanently stale.
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
    find_matching_tables,
    logger,
    now_iso,
    parse_flexible_date,
)

# Keyword groups used to identify Chittorgarh's single IPO table on a page.
_CHITTORGARH_KEYWORD_GROUPS: Sequence[Sequence[str]] = (
    ("ipo", "company", "issue"),
    ("open",),
    ("close",),
    ("price", "band"),
    ("lot",),
)

# Keyword groups used to identify Groww's (possibly several) IPO tables.
_GROWW_KEYWORD_GROUPS: Sequence[Sequence[str]] = (
    ("company",),
    ("open date",),
    ("close date",),
)

# Groww's "Closed IPOs" table carries these columns (listing price /
# performance) that its "Open"/"Upcoming" tables don't. We only want IPOs
# that are currently open or upcoming for "new IPO" alerts -- a company
# that already listed weeks ago isn't news -- so any table with these
# columns is skipped entirely.
_GROWW_CLOSED_TABLE_MARKERS: Sequence[str] = ("listing price", "performance")

# Keyword lists used to map a table's columns to our internal fields.
_FIELD_KEYWORDS: Dict[str, Sequence[str]] = {
    "company_name": ("ipo name", "company", "issuer", "ipo"),
    "ipo_type": ("type",),
    "price_band": ("price band", "issue price", "price", "band"),
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
        """Fetch from every configured source. Never raises -- logs and
        returns whatever sources succeeded (possibly an empty list).

        Groww is tried first since it's the only source confirmed reachable
        at build time; Chittorgarh is tried too and merged in (it's blocked
        as of writing, but costs nothing to keep trying -- and it carries a
        couple of fields Groww's page doesn't show). Records are deduped by
        IPO name, keeping whichever source saw them first.
        """
        records: List[IPORecord] = []
        seen_names: set = set()

        for record in self._fetch_groww():
            key = record.ipo_name.strip().lower()
            if key not in seen_names:
                seen_names.add(key)
                records.append(record)

        for record in self._fetch_chittorgarh_segment(
            settings.chittorgarh_mainboard_url, "Mainboard"
        ) + self._fetch_chittorgarh_segment(settings.chittorgarh_sme_url, "SME"):
            key = record.ipo_name.strip().lower()
            if key not in seen_names:
                seen_names.add(key)
                records.append(record)

        logger.info("Scraper collected %d IPO listing(s) this cycle", len(records))
        return records

    # ------------------------------------------------------------------
    # Groww (primary -- confirmed reachable)
    # ------------------------------------------------------------------
    def _fetch_groww(self) -> List[IPORecord]:
        url = settings.groww_ipo_url
        try:
            html = fetch_html(url)
        except (FetchError, Exception) as exc:  # noqa: BLE001
            logger.error("Failed to fetch IPO list from Groww (%s): %s", url, exc)
            return []

        tables = extract_tables(html)
        if not tables:
            logger.warning("No HTML tables found on Groww IPO page (%s)", url)
            return []

        matching_tables = find_matching_tables(tables, _GROWW_KEYWORD_GROUPS, min_score=2)
        if not matching_tables:
            logger.warning(
                "Could not identify any IPO table on Groww (%s) -- site markup "
                "may have changed; update scraper.py keyword groups if this "
                "persists.", url,
            )
            return []

        records: List[IPORecord] = []
        for table in matching_tables:
            headers = " | ".join(str(c).lower() for c in table.columns)
            if any(marker in headers for marker in _GROWW_CLOSED_TABLE_MARKERS):
                continue  # skip the "Closed IPOs" table -- already listed, not news

            column_map = build_column_map(list(table.columns), _FIELD_KEYWORDS)
            if "company_name" not in column_map:
                continue
            for _, row in table.iterrows():
                record = self._row_to_record(row, column_map, "Mainboard", url)
                if record is not None:
                    records.append(record)
        return records

    # ------------------------------------------------------------------
    # Chittorgarh (secondary -- best-effort, known to be blocked at times)
    # ------------------------------------------------------------------
    def _fetch_chittorgarh_segment(self, url: str, default_ipo_type: str) -> List[IPORecord]:
        try:
            html = fetch_html(url)
        except (FetchError, Exception) as exc:  # noqa: BLE001
            logger.error("Failed to fetch %s IPO list from %s: %s", default_ipo_type, url, exc)
            return []

        tables = extract_tables(html)
        if not tables:
            logger.warning("No HTML tables found on %s (%s)", url, default_ipo_type)
            return []

        table = find_best_table(tables, _CHITTORGARH_KEYWORD_GROUPS)
        if table is None:
            logger.warning(
                "Could not identify the IPO table on %s (%s) -- site markup may "
                "have changed; update scraper.py keyword groups if this persists.",
                url, default_ipo_type,
            )
            return []

        column_map = build_column_map(list(table.columns), _FIELD_KEYWORDS)
        if "company_name" not in column_map:
            logger.warning("IPO table on %s has no recognizable name column", url)
            return []

        records: List[IPORecord] = []
        for _, row in table.iterrows():
            record = self._row_to_record(row, column_map, default_ipo_type, url)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _row_to_record(
        row: "pd.Series", column_map: Dict[str, str], default_ipo_type: str, source_url: str
    ) -> "IPORecord | None":
        company_name = clean_text(row.get(column_map.get("company_name", ""), ""))
        if not company_name or company_name.lower() in {"nan", "ipo name", "company name", ""}:
            return None

        def field(name: str) -> str:
            col = column_map.get(name)
            return clean_text(row.get(col, "")) if col else ""

        ipo_type = field("ipo_type") or default_ipo_type

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
