"""Single-cycle entry point for one-shot / cron-style hosting.

``main.py`` runs a persistent process with its own internal scheduler --
that's right for a VPS, Raspberry Pi, or Docker host that stays powered on.
Some free hosting (e.g. a GitHub Actions scheduled workflow) instead spins
up a fresh, ephemeral environment on a cron schedule and tears it down
after each run. This script supports that model: it performs exactly one
poll cycle (scrape, compare, notify) using the same ``IPOMonitor`` business
logic, then exits.

Usage:
    python run_once.py

The SQLite database at ``database/ipo_bot.db`` must be persisted between
invocations by whatever is calling this script (see the GitHub Actions
workflow in .github/workflows/ipo-bot.yml, which commits the updated file
back to the repo after each run).
"""

from __future__ import annotations

import sys

from config import settings
from database import Database
from gmp import GMPScraper
from scheduler import IPOMonitor
from scraper import IPOScraper
from telegram import TelegramNotifier
from utils import logger


def main() -> int:
    logger.info("Running a single IPO/GMP poll cycle (one-shot mode)")

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

    monitor.run_cycle()
    logger.info("One-shot cycle complete, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
