"""Central configuration for the IPO bot.

All runtime configuration is loaded from environment variables (via a .env
file) with sane defaults. Nothing else in the project should read os.environ
directly -- import from here instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_list(name: str, default: List[str]) -> List[str]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable application settings, populated once at import time."""

    # --- Telegram -----------------------------------------------------
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.getenv("CHAT_ID", ""))
    telegram_api_base: str = field(
        default_factory=lambda: os.getenv(
            "TELEGRAM_API_BASE", "https://api.telegram.org"
        )
    )

    # --- Paths ----------------------------------------------------------
    base_dir: Path = BASE_DIR
    db_path: Path = field(
        default_factory=lambda: BASE_DIR / os.getenv("DB_PATH", "database/ipo_bot.db")
    )
    log_dir: Path = field(
        default_factory=lambda: BASE_DIR / os.getenv("LOG_DIR", "logs")
    )
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "app.log"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    csv_export_dir: Path = field(
        default_factory=lambda: BASE_DIR / os.getenv("CSV_EXPORT_DIR", "exports")
    )

    # --- Scheduler --------------------------------------------------------
    poll_interval_minutes: int = field(
        default_factory=lambda: _get_int("POLL_INTERVAL_MINUTES", 10)
    )
    morning_summary_time: str = field(
        default_factory=lambda: os.getenv("MORNING_SUMMARY_TIME", "08:00")
    )
    evening_summary_time: str = field(
        default_factory=lambda: os.getenv("EVENING_SUMMARY_TIME", "20:00")
    )
    run_immediately_on_start: bool = field(
        default_factory=lambda: _get_bool("RUN_IMMEDIATELY_ON_START", True)
    )

    # --- GMP change thresholds -------------------------------------------
    gmp_abs_threshold: float = field(
        default_factory=lambda: _get_float("GMP_ABS_THRESHOLD", 5.0)
    )
    gmp_pct_threshold: float = field(
        default_factory=lambda: _get_float("GMP_PCT_THRESHOLD", 5.0)
    )

    # --- Networking -------------------------------------------------------
    request_timeout: int = field(
        default_factory=lambda: _get_int("REQUEST_TIMEOUT", 20)
    )
    max_retries: int = field(default_factory=lambda: _get_int("MAX_RETRIES", 3))
    retry_backoff_seconds: float = field(
        default_factory=lambda: _get_float("RETRY_BACKOFF_SECONDS", 3.0)
    )
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
    )

    # --- Data sources -------------------------------------------------------
    groww_ipo_url: str = field(
        default_factory=lambda: os.getenv("GROWW_IPO_URL", "https://groww.in/ipo")
    )
    chittorgarh_mainboard_url: str = field(
        default_factory=lambda: os.getenv(
            "CHITTORGARH_MAINBOARD_URL",
            "https://www.chittorgarh.com/report/mainline-ipo-list-in-india-bse-nse/83/",
        )
    )
    chittorgarh_sme_url: str = field(
        default_factory=lambda: os.getenv(
            "CHITTORGARH_SME_URL",
            "https://www.chittorgarh.com/report/sme-ipo-list-in-india-bse-sme-nse-emerge/84/",
        )
    )
    investorgain_gmp_url: str = field(
        default_factory=lambda: os.getenv(
            "INVESTORGAIN_GMP_URL",
            "https://www.investorgain.com/report/live-ipo-gmp/331/",
        )
    )
    ipowatch_gmp_url: str = field(
        default_factory=lambda: os.getenv(
            "IPOWATCH_GMP_URL",
            "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/",
        )
    )

    # --- Feature toggles -------------------------------------------------
    enable_morning_summary: bool = field(
        default_factory=lambda: _get_bool("ENABLE_MORNING_SUMMARY", True)
    )
    enable_evening_summary: bool = field(
        default_factory=lambda: _get_bool("ENABLE_EVENING_SUMMARY", True)
    )
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Kolkata"))

    def validate(self) -> None:
        """Raise a clear error if mandatory settings are missing."""
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.chat_id:
            missing.append("CHAT_ID")
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): "
                f"{', '.join(missing)}. Copy .env.example to .env and fill them in."
            )


settings = Settings()
