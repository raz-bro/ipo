"""Entry point for the IPO monitoring bot.

Usage:
    python main.py

Runs 24/7: every ``POLL_INTERVAL_MINUTES`` (default 10) it re-scrapes IPO
listings + GMP data, detects changes against the SQLite database, and sends
Telegram alerts for new IPOs, GMP moves, and open/close/allotment/listing
milestones. Also sends morning/evening summaries if enabled.
"""

from __future__ import annotations

import signal
import sys
import time
from types import FrameType
from typing import Optional

from config import settings
from database import Database
from gmp import GMPScraper
from scheduler import IPOMonitor, build_scheduler
from scraper import IPOScraper
from telegram import TelegramNotifier
from utils import logger

_shutdown_requested = False


def _handle_shutdown_signal(signum: int, frame: Optional[FrameType]) -> None:
    global _shutdown_requested
    logger.info("Received signal %s, shutting down gracefully...", signum)
    _shutdown_requested = True


def main() -> int:
    logger.info("=" * 70)
    logger.info("Starting IPO Monitoring Bot")
    logger.info("=" * 70)

    try:
        settings.validate()
    except RuntimeError as exc:
        logger.error(str(exc))
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    db = Database()
    notifier = TelegramNotifier()
    monitor = IPOMonitor(
        db=db,
        notifier=notifier,
        ipo_scraper=IPOScraper(),
        gmp_scraper=GMPScraper(),
    )

    scheduler = build_scheduler(monitor)

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    scheduler.start()
    logger.info(
        "Scheduler started. Polling every %d minute(s). Press Ctrl+C to stop.",
        settings.poll_interval_minutes,
    )

    try:
        while not _shutdown_requested:
            time.sleep(1)
    finally:
        logger.info("Stopping scheduler...")
        scheduler.shutdown(wait=False)
        logger.info("IPO Monitoring Bot stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
