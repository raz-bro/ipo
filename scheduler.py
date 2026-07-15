"""Business logic + APScheduler job wiring.

``IPOMonitor`` holds all of the "compare scraped data to the database and
decide what to notify" logic. ``build_scheduler`` wires its methods up to
APScheduler jobs at the configured intervals. Keeping the decision logic in
a plain class (rather than free functions bound directly to the scheduler)
makes it straightforward to unit test independently of APScheduler.
"""

from __future__ import annotations

from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from database import Database, IPORecord
from gmp import GMPScraper
from scraper import IPOScraper
from telegram import TelegramNotifier
from utils import logger, today_iso

# Milestone notification types are sent at most once per IPO.
_MILESTONE_TYPES = ("open", "close", "allotment", "listing")


class IPOMonitor:
    """Orchestrates scraping, change-detection, and notification dispatch."""

    def __init__(
        self,
        db: Database,
        notifier: TelegramNotifier,
        ipo_scraper: IPOScraper,
        gmp_scraper: GMPScraper,
    ) -> None:
        self.db = db
        self.notifier = notifier
        self.ipo_scraper = ipo_scraper
        self.gmp_scraper = gmp_scraper

    # ------------------------------------------------------------------
    # Core 10-minute cycle
    # ------------------------------------------------------------------
    def run_cycle(self) -> None:
        """One full poll cycle: refresh IPO listings + GMP, notify on change."""
        logger.info("Starting IPO/GMP poll cycle")
        try:
            listings = self.ipo_scraper.fetch_all()
        except Exception:
            logger.exception("Unhandled error while scraping IPO listings")
            listings = []

        try:
            gmp_quotes = self.gmp_scraper.fetch_all()
        except Exception:
            logger.exception("Unhandled error while scraping GMP data")
            gmp_quotes = []

        for listing in listings:
            try:
                self._process_listing(listing, gmp_quotes)
            except Exception:
                logger.exception("Failed processing listing %s", listing.ipo_name)

        self.check_milestones()
        self.check_summaries()
        logger.info("Poll cycle complete (%d listings processed)", len(listings))

    def _process_listing(self, listing: IPORecord, gmp_quotes: list) -> None:
        existing = self.db.get_ipo_by_name(listing.ipo_name)
        is_new = existing is None

        quote = self.gmp_scraper.match_quote(listing.company_name, gmp_quotes)
        if quote is not None:
            listing.current_gmp = quote.gmp
            listing.kostak = quote.kostak
            listing.subject_to_sauda = quote.subject_to_sauda
        elif existing is not None:
            # Keep last known GMP if this cycle's GMP source had no match.
            listing.current_gmp = existing.current_gmp
            listing.kostak = existing.kostak
            listing.subject_to_sauda = existing.subject_to_sauda

        previous_gmp = existing.current_gmp if existing else None
        listing.gmp_change = (
            round(listing.current_gmp - previous_gmp, 2)
            if listing.current_gmp is not None and previous_gmp is not None
            else None
        )

        record = self.db.upsert_ipo(listing)

        if is_new:
            logger.info("New IPO detected: %s", record.company_name)
            self.db.add_gmp_history(record.id, record.current_gmp, record.kostak, record.subject_to_sauda)
            if self.notifier.notify_new_ipo(record):
                self.db.record_notification(record.id, "new_ipo")
            return

        self._maybe_notify_gmp_change(record, previous_gmp)

    def _maybe_notify_gmp_change(self, record: IPORecord, previous_gmp) -> None:
        if record.current_gmp is None:
            return
        if previous_gmp is None:
            self.db.add_gmp_history(record.id, record.current_gmp, record.kostak, record.subject_to_sauda)
            return

        diff = abs(record.current_gmp - previous_gmp)
        pct = (diff / previous_gmp * 100) if previous_gmp else 0

        if diff == 0:
            return

        self.db.add_gmp_history(record.id, record.current_gmp, record.kostak, record.subject_to_sauda)

        if diff >= settings.gmp_abs_threshold or pct >= settings.gmp_pct_threshold:
            logger.info(
                "GMP change for %s: %s -> %s (diff=%.2f, pct=%.1f%%)",
                record.company_name, previous_gmp, record.current_gmp, diff, pct,
            )
            if self.notifier.notify_gmp_update(record, previous_gmp, record.current_gmp):
                self.db.record_notification(
                    record.id, "gmp_update", details=f"{previous_gmp}->{record.current_gmp}"
                )

    # ------------------------------------------------------------------
    # Date-driven milestone alerts
    # ------------------------------------------------------------------
    def check_milestones(self) -> None:
        today = today_iso()
        for record in self.db.list_ipos():
            self._check_milestone(record, "open", record.open_date, today)
            self._check_milestone(record, "close", record.close_date, today)
            self._check_milestone(record, "allotment", record.allotment_date, today)
            self._check_milestone(record, "listing", record.listing_date, today)

    def _check_milestone(self, record: IPORecord, kind: str, date_value, today: str) -> None:
        if not date_value or date_value != today:
            return
        if self.db.has_notification(record.id, kind):
            return
        if self.notifier.notify_status(record, kind):
            self.db.record_notification(record.id, kind)
            logger.info("Sent '%s' milestone alert for %s", kind, record.company_name)

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------
    def _active_ipos(self) -> List[IPORecord]:
        today = today_iso()
        active = []
        for record in self.db.list_ipos():
            if record.listing_date and record.listing_date < today:
                continue
            active.append(record)
        return active

    def send_morning_summary(self) -> None:
        notif_type = f"morning_summary_{today_iso()}"
        if self.db.has_notification(None, notif_type):
            return
        records = self._active_ipos()
        if self.notifier.notify_summary(records, "☀️ <b>GOOD MORNING - IPO SUMMARY</b>"):
            self.db.record_notification(None, notif_type)
            logger.info("Morning summary sent (%d active IPOs)", len(records))

    def send_evening_summary(self) -> None:
        notif_type = f"evening_summary_{today_iso()}"
        if self.db.has_notification(None, notif_type):
            return
        records = self._active_ipos()
        if self.notifier.notify_summary(records, "🌙 <b>DAILY IPO SUMMARY</b>"):
            self.db.record_notification(None, notif_type)
            logger.info("Evening summary sent (%d active IPOs)", len(records))

    def check_summaries(self) -> None:
        """Time-of-day-based summary check, safe to call every poll cycle.

        ``send_morning_summary``/``send_evening_summary`` are idempotent
        (guarded by a per-day notification record), so calling this on
        every cycle is harmless -- it's what makes summaries work correctly
        under a one-shot invocation model (e.g. a GitHub Actions cron job)
        where there's no persistent process to run a precise APScheduler
        CronTrigger. Under a persistent deployment, the CronTrigger jobs
        registered in ``build_scheduler`` fire at the exact configured time;
        this is just a coarser (within one poll interval) fallback/primary
        path depending on how the bot is run.
        """
        now = datetime.now(ZoneInfo(settings.timezone))
        if settings.enable_morning_summary:
            hour, minute = _parse_hhmm(settings.morning_summary_time)
            if (now.hour, now.minute) >= (hour, minute):
                self.send_morning_summary()
        if settings.enable_evening_summary:
            hour, minute = _parse_hhmm(settings.evening_summary_time)
            if (now.hour, now.minute) >= (hour, minute):
                self.send_evening_summary()


def build_scheduler(monitor: IPOMonitor) -> BackgroundScheduler:
    """Create and configure (but do not start) the APScheduler instance."""
    scheduler = BackgroundScheduler(timezone=settings.timezone)

    scheduler.add_job(
        monitor.run_cycle,
        trigger="interval",
        minutes=settings.poll_interval_minutes,
        id="poll_cycle",
        next_run_time=datetime.now() if settings.run_immediately_on_start else None,
        max_instances=1,
        coalesce=True,
    )

    if settings.enable_morning_summary:
        hour, minute = _parse_hhmm(settings.morning_summary_time)
        scheduler.add_job(
            monitor.send_morning_summary,
            trigger=CronTrigger(hour=hour, minute=minute),
            id="morning_summary",
            max_instances=1,
            coalesce=True,
        )

    if settings.enable_evening_summary:
        hour, minute = _parse_hhmm(settings.evening_summary_time)
        scheduler.add_job(
            monitor.send_evening_summary,
            trigger=CronTrigger(hour=hour, minute=minute),
            id="evening_summary",
            max_instances=1,
            coalesce=True,
        )

    return scheduler


def _parse_hhmm(value: str) -> tuple:
    try:
        hour_str, minute_str = value.split(":")
        return int(hour_str), int(minute_str)
    except ValueError:
        logger.warning("Invalid HH:MM time %r, defaulting to 08:00", value)
        return 8, 0
