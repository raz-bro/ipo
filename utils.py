"""Shared helpers: logging setup, retry decorator, and parsing utilities.

Nothing here talks to the network or the database directly -- it only
provides small, dependency-free building blocks used by the rest of the
project.
"""

from __future__ import annotations

import functools
import io
import logging
import re
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Callable, Dict, List, Optional, Sequence, TypeVar

import pandas as pd
import requests

from config import settings

T = TypeVar("T")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def setup_logging() -> logging.Logger:
    """Configure root application logging to console + rotating file.

    Safe to call multiple times (e.g. from tests) -- handlers are only
    attached once.
    """
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ipo_bot")

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        settings.log_dir / settings.log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger


logger = setup_logging()


def retry(
    max_attempts: Optional[int] = None,
    backoff_seconds: Optional[float] = None,
    exceptions: tuple = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry a function with exponential backoff on the given exceptions.

    Defaults are pulled from settings so every network call in the project
    behaves consistently unless overridden.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            attempts = max_attempts or settings.max_retries
            delay = backoff_seconds or settings.retry_backoff_seconds
            last_exc: Optional[Exception] = None

            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001 - deliberately broad
                    last_exc = exc
                    logger.warning(
                        "%s failed (attempt %d/%d): %s",
                        func.__name__, attempt, attempts, exc,
                    )
                    if attempt < attempts:
                        time.sleep(delay * attempt)  # linear backoff growth
            logger.error(
                "%s failed after %d attempts, giving up: %s",
                func.__name__, attempts, last_exc,
            )
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


def safe_float(value: object) -> Optional[float]:
    """Extract a float from noisy strings like '₹145', '1,450', '12%', '--'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text or text in {"-", "--", "NA", "N/A", "na"}:
        return None

    match = re.search(r"-?\d[\d,]*\.?\d*", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def clean_text(value: object) -> str:
    """Collapse whitespace and strip a scraped cell value to plain text."""
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_flexible_date(value: object, default_year: Optional[int] = None) -> Optional[str]:
    """Parse loosely-formatted IPO dates into an ISO 'YYYY-MM-DD' string.

    Handles formats commonly seen on IPO tracker sites, e.g.:
    '18 Jul', '18-Jul-2026', 'Jul 18, 2026', '18/07/2026', '2026-07-18'.
    Returns None if the value cannot be parsed.
    """
    text = clean_text(value)
    if not text or text.lower() in {"-", "tba", "n/a", "na", "tentative"}:
        return None

    year = default_year or datetime.now().year

    iso_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if iso_match:
        y, m, d = (int(g) for g in iso_match.groups())
        return _safe_date(y, m, d)

    slash_match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", text)
    if slash_match:
        d, m, y = slash_match.groups()
        y_int = int(y) if len(y) == 4 else 2000 + int(y)
        return _safe_date(y_int, int(m), int(d))

    text_norm = text.replace(",", " ").replace("'", " ")
    tokens = [t for t in re.split(r"[\s\-]+", text_norm) if t]

    day = month = None
    year_found = None
    for tok in tokens:
        tok_low = tok.lower()[:3]
        if tok_low in _MONTHS:
            month = _MONTHS[tok_low]
        elif re.match(r"^\d{4}$", tok):
            year_found = int(tok)
        elif re.match(r"^\d{1,2}(st|nd|rd|th)?$", tok):
            day = int(re.sub(r"(st|nd|rd|th)$", "", tok))

    if day and month:
        return _safe_date(year_found or year, month, day)

    return None


def _safe_date(year: int, month: int, day: int) -> Optional[str]:
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def now_iso() -> str:
    """Current local timestamp formatted for storage/logging."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )
    return _session


class FetchError(Exception):
    """Raised when a URL cannot be fetched after all retries."""


@retry(exceptions=(requests.RequestException, FetchError))
def fetch_html(url: str, timeout: Optional[int] = None) -> str:
    """GET a URL and return its response body as text, with retries.

    Raises FetchError on non-2xx responses (including 429 rate limiting,
    which is treated like any other transient failure and retried) and
    requests.RequestException on network-level failures (timeouts, DNS,
    connection refused, etc.).
    """
    session = _get_session()
    response = session.get(url, timeout=timeout or settings.request_timeout)

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "5")
        logger.warning("Rate limited by %s, Retry-After=%s", url, retry_after)
        try:
            time.sleep(min(float(retry_after), 30))
        except ValueError:
            time.sleep(5)
        raise FetchError(f"Rate limited (429) fetching {url}")

    if response.status_code >= 400:
        raise FetchError(f"HTTP {response.status_code} fetching {url}")

    return response.text


def extract_tables(html: str) -> List[pd.DataFrame]:
    """Extract every HTML <table> on a page as a DataFrame.

    IPO tracker sites (Chittorgarh, InvestorGain, IPO Watch, ...) publish
    their data as plain HTML tables, and their exact CSS classes/ids change
    often. Parsing every table generically and then picking the right one
    by column-header content (see ``find_best_table``) is far more resilient
    to markup churn than hardcoded selectors.
    """
    try:
        return pd.read_html(io.StringIO(html))
    except ValueError:
        # No tables found on the page at all.
        return []


def find_best_table(
    tables: Sequence[pd.DataFrame], required_keyword_groups: Sequence[Sequence[str]]
) -> Optional[pd.DataFrame]:
    """Pick the table whose headers best match the expected IPO/GMP columns.

    ``required_keyword_groups`` is a list of keyword groups, e.g.
    ``[["ipo", "company"], ["gmp", "premium"], ["price"]]``. A table scores
    one point per group that has at least one matching column header.
    The table with the highest score (and at least one match) wins.
    """
    best_table: Optional[pd.DataFrame] = None
    best_score = 0

    for table in tables:
        headers = " | ".join(str(c).lower() for c in table.columns)
        score = sum(
            1
            for group in required_keyword_groups
            if any(keyword in headers for keyword in group)
        )
        if score > best_score:
            best_score = score
            best_table = table

    if best_score == 0:
        return None
    return best_table


def find_matching_tables(
    tables: Sequence[pd.DataFrame],
    required_keyword_groups: Sequence[Sequence[str]],
    min_score: int = 1,
) -> List[pd.DataFrame]:
    """Like ``find_best_table``, but returns every table that scores at
    least ``min_score`` instead of only the single best one.

    Some sites (e.g. Groww's IPO page) split the same kind of data across
    several separate tables on one page (Open / Upcoming / Closed IPOs).
    Picking only "the best" table would silently drop the others.
    """
    matches: List[pd.DataFrame] = []
    for table in tables:
        headers = " | ".join(str(c).lower() for c in table.columns)
        score = sum(
            1
            for group in required_keyword_groups
            if any(keyword in headers for keyword in group)
        )
        if score >= min_score:
            matches.append(table)
    return matches


def match_column(columns: Sequence[str], keywords: Sequence[str]) -> Optional[str]:
    """Find the first column whose (lowercased) name contains any keyword."""
    for col in columns:
        col_low = str(col).lower()
        if any(keyword in col_low for keyword in keywords):
            return col
    return None


def parse_price_band(text: object) -> "tuple[Optional[float], Optional[float]]":
    """Parse a price band string like '₹320-340' or '₹320 to ₹340' into
    (low, high) floats. Returns (None, None) if it can't be parsed, or
    (value, value) if only a single price is present."""
    clean = clean_text(text)
    numbers = re.findall(r"\d[\d,]*\.?\d*", clean)
    numbers = [n.replace(",", "") for n in numbers]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        val = float(numbers[0])
        return val, val
    return float(numbers[0]), float(numbers[-1])


def build_column_map(
    columns: Sequence[str], field_keywords: Dict[str, Sequence[str]]
) -> Dict[str, str]:
    """Map internal field names to actual DataFrame column names.

    ``field_keywords`` looks like ``{"gmp": ["gmp"], "company_name": ["ipo", "company"]}``.
    Returns only the fields that were successfully matched.
    """
    mapping: Dict[str, str] = {}
    for field_name, keywords in field_keywords.items():
        col = match_column(columns, keywords)
        if col is not None:
            mapping[field_name] = col
    return mapping
