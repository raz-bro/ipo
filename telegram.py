"""Telegram Bot API notifier.

Formats and sends every alert type the bot produces: new IPO detected, GMP
updated, IPO open/close/allotment/listing day, and the morning/evening
summaries.
"""

from __future__ import annotations

import time
from typing import List, Optional

import requests

from config import settings
from database import IPORecord
from utils import logger, parse_price_band, retry


class TelegramNotifier:
    """Sends formatted alerts to one or more Telegram chats via Bot API.

    Supports multiple recipients (e.g. your personal DM plus a group chat)
    via ``settings.chat_ids`` -- a message is sent to every configured chat
    id independently, so one recipient failing (e.g. the bot got removed
    from a group) doesn't block delivery to the others.
    """

    def __init__(
        self, bot_token: Optional[str] = None, chat_ids: Optional[List[str]] = None
    ) -> None:
        self.bot_token = bot_token or settings.bot_token
        self.chat_ids = chat_ids if chat_ids is not None else settings.chat_ids
        self._api_url = f"{settings.telegram_api_base}/bot{self.bot_token}/sendMessage"

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------
    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to every configured chat id.

        Returns True if delivery succeeded to at least one recipient (so a
        single broken recipient, e.g. the bot losing group access, doesn't
        prevent the notification from being marked as sent and cause it to
        be re-sent every cycle to the recipients that DO still work).
        """
        if not self.chat_ids:
            logger.error("No CHAT_ID configured, cannot send Telegram message")
            return False

        any_success = False
        for chat_id in self.chat_ids:
            try:
                if self._send_to_chat(chat_id, text, parse_mode):
                    any_success = True
            except Exception:  # noqa: BLE001
                logger.exception("Failed to send Telegram message to chat %s", chat_id)
        return any_success

    @retry(exceptions=(requests.RequestException,))
    def _send_to_chat(self, chat_id: str, text: str, parse_mode: str) -> bool:
        """Send to a single chat id, handling Telegram's 429 rate limiting.

        Returns True on success, False if Telegram permanently rejected the
        request (e.g. bad chat id) -- those are not worth retrying forever.
        """
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        response = requests.post(self._api_url, data=payload, timeout=settings.request_timeout)

        if response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 5)
            logger.warning("Telegram rate limit hit, sleeping %ss", retry_after)
            time.sleep(retry_after)
            raise requests.RequestException("Rate limited by Telegram (429)")

        if response.status_code == 400:
            logger.error("Telegram rejected message for chat %s (400): %s", chat_id, response.text)
            return False

        response.raise_for_status()
        logger.info("Telegram message sent to %s (%d chars)", chat_id, len(text))
        return True

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------
    @staticmethod
    def _expected_listing_gain(record: IPORecord) -> Optional[float]:
        _, high = parse_price_band(record.price_band)
        if not high or record.current_gmp is None:
            return None
        return round((record.current_gmp / high) * 100, 1)

    def format_new_ipo(self, record: IPORecord) -> str:
        gain = self._expected_listing_gain(record)
        gain_line = f"{gain}%" if gain is not None else "N/A"
        gmp_line = f"₹{record.current_gmp:g}" if record.current_gmp is not None else "N/A"
        return (
            "🚀 <b>NEW IPO DETECTED</b>\n\n"
            f"<b>Company:</b>\n{record.company_name}\n\n"
            f"<b>IPO Type:</b>\n{record.ipo_type or 'N/A'}\n\n"
            f"<b>Price Band:</b>\n{record.price_band or 'N/A'}\n\n"
            f"<b>Issue Size:</b>\n{record.issue_size or 'N/A'}\n\n"
            f"<b>Open:</b>\n{record.open_date or 'TBA'}\n\n"
            f"<b>Close:</b>\n{record.close_date or 'TBA'}\n\n"
            f"<b>Current GMP:</b>\n{gmp_line}\n\n"
            f"<b>Expected Listing Gain:</b>\n{gain_line}\n\n"
            f"<b>Lot Size:</b>\n{record.lot_size or 'N/A'}\n\n"
            f"<b>Listing:</b>\n{record.listing_date or 'TBA'}\n\n"
            f"<b>Registrar:</b>\n{record.registrar or 'N/A'}\n\n"
            f"<b>Exchange:</b>\n{record.exchange or 'N/A'}\n\n"
            f"<b>Source:</b>\n{record.source_url or 'N/A'}"
        )

    def format_gmp_update(
        self, record: IPORecord, old_gmp: Optional[float], new_gmp: Optional[float]
    ) -> str:
        diff = (new_gmp or 0) - (old_gmp or 0)
        sign = "+" if diff >= 0 else ""
        old_gmp_line = f"₹{old_gmp:g}" if old_gmp is not None else "N/A"
        new_gmp_line = f"₹{new_gmp:g}" if new_gmp is not None else "N/A"
        return (
            "📈 <b>GMP UPDATED</b>\n\n"
            f"<b>IPO:</b>\n{record.company_name}\n\n"
            f"<b>Old GMP:</b>\n{old_gmp_line}\n\n"
            f"<b>New GMP:</b>\n{new_gmp_line}\n\n"
            f"<b>Difference:</b>\n{sign}₹{diff:g}\n\n"
            f"<b>Time:</b>\n{time.strftime('%I:%M %p')}"
        )

    def format_status_alert(self, record: IPORecord, kind: str) -> str:
        headers = {
            "open": "🟢 <b>IPO OPEN TODAY</b>",
            "close": "🔴 <b>IPO CLOSES TODAY</b>",
            "allotment": "🎯 <b>IPO ALLOTMENT TODAY</b>",
            "listing": "📊 <b>IPO LISTING TODAY</b>",
        }
        header = headers[kind]
        lines = [
            header,
            "",
            f"<b>Company:</b>\n{record.company_name}",
            "",
            f"<b>IPO Type:</b>\n{record.ipo_type or 'N/A'}",
            "",
            f"<b>Price Band:</b>\n{record.price_band or 'N/A'}",
        ]
        if record.current_gmp is not None:
            lines += ["", f"<b>Current GMP:</b>\n₹{record.current_gmp:g}"]
        if kind == "close":
            lines += ["", f"<b>Close Date:</b>\n{record.close_date or 'N/A'}"]
        if kind == "allotment":
            lines += ["", f"<b>Allotment Date:</b>\n{record.allotment_date or 'N/A'}"]
        if kind == "listing":
            lines += ["", f"<b>Listing Date:</b>\n{record.listing_date or 'N/A'}"]
            lines += ["", f"<b>Exchange:</b>\n{record.exchange or 'N/A'}"]
        lines += ["", f"<b>Source:</b>\n{record.source_url or 'N/A'}"]
        return "\n".join(lines)

    def format_summary(self, records: List[IPORecord], title: str) -> str:
        if not records:
            return f"{title}\n\nNo active IPOs to report right now."

        lines = [title, ""]
        for r in records:
            gmp_line = f"₹{r.current_gmp:g}" if r.current_gmp is not None else "N/A"
            lines.append(
                f"• <b>{r.company_name}</b> ({r.ipo_type or 'N/A'})\n"
                f"  Price: {r.price_band or 'N/A'} | GMP: {gmp_line}\n"
                f"  Open: {r.open_date or 'TBA'} | Close: {r.close_date or 'TBA'} | "
                f"Listing: {r.listing_date or 'TBA'}"
            )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # High level send helpers
    # ------------------------------------------------------------------
    def notify_new_ipo(self, record: IPORecord) -> bool:
        return self.send_message(self.format_new_ipo(record))

    def notify_gmp_update(
        self, record: IPORecord, old_gmp: Optional[float], new_gmp: Optional[float]
    ) -> bool:
        return self.send_message(self.format_gmp_update(record, old_gmp, new_gmp))

    def notify_status(self, record: IPORecord, kind: str) -> bool:
        return self.send_message(self.format_status_alert(record, kind))

    def notify_summary(self, records: List[IPORecord], title: str) -> bool:
        return self.send_message(self.format_summary(records, title))
