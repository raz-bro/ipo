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
from utils import expected_profit_amount, logger, min_investment_amount, parse_price_band, retry


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
    # Message formatting -- everything is batched: at most one message per
    # category per poll cycle (new IPOs / GMP moves / today's milestones),
    # listing every affected IPO in that one message rather than firing a
    # separate message per IPO.
    # ------------------------------------------------------------------
    @staticmethod
    def _expected_listing_gain(record: IPORecord) -> Optional[float]:
        _, high = parse_price_band(record.price_band)
        if not high or record.current_gmp is None:
            return None
        return round((record.current_gmp / high) * 100, 1)

    @staticmethod
    def _investment_line(record: IPORecord) -> str:
        amount = min_investment_amount(record.price_band, record.lot_size)
        return f" | Invest: ₹{amount:,}" if amount is not None else ""

    @staticmethod
    def _profit_line(record: IPORecord) -> str:
        profit = expected_profit_amount(record.current_gmp, record.lot_size)
        return f" | Profit: ₹{profit:,}" if profit is not None else ""

    def _new_ipo_line(self, record: IPORecord) -> str:
        gain = self._expected_listing_gain(record)
        gain_line = f" | Gain: {gain}%" if gain is not None else ""
        gmp_line = f"₹{record.current_gmp:g}" if record.current_gmp is not None else "N/A"
        lot_line = f" | Lot: {record.lot_size}" if record.lot_size else ""
        return (
            f"• <b>{record.company_name}</b> ({record.ipo_type or 'N/A'})\n"
            f"  {record.price_band or 'N/A'}{lot_line}{self._investment_line(record)}\n"
            f"  GMP: {gmp_line}{gain_line}{self._profit_line(record)}\n"
            f"  {record.open_date or 'TBA'}→{record.close_date or 'TBA'}"
        )

    def format_new_ipos_batch(self, records: List[IPORecord]) -> str:
        title = f"🚀 <b>{len(records)} NEW IPO{'s' if len(records) != 1 else ''}</b>"
        lines = [title] + [self._new_ipo_line(r) for r in records]
        return "\n\n".join(lines)

    def format_gmp_updates_batch(
        self, events: List["tuple[IPORecord, Optional[float], Optional[float]]"]
    ) -> str:
        title = f"📈 <b>GMP UPDATE ({len(events)})</b>"
        lines = [title]
        for record, old_gmp, new_gmp in events:
            diff = (new_gmp or 0) - (old_gmp or 0)
            sign = "+" if diff >= 0 else ""
            old_line = f"₹{old_gmp:g}" if old_gmp is not None else "N/A"
            new_line = f"₹{new_gmp:g}" if new_gmp is not None else "N/A"
            lines.append(
                f"• <b>{record.company_name}</b>: {old_line} → {new_line} ({sign}₹{diff:g})"
            )
        return "\n\n".join(lines)

    def format_milestones_batch(self, events: List["tuple[IPORecord, str]"]) -> str:
        emojis = {"open": "🟢", "close": "🔴", "allotment": "🎯", "listing": "📊"}
        labels = {"open": "Open", "close": "Closes", "allotment": "Allotment", "listing": "Listing"}
        title = "📅 <b>TODAY'S IPO EVENTS</b>"
        lines = [title]
        for record, kind in events:
            gmp_line = f" | GMP: ₹{record.current_gmp:g}" if record.current_gmp is not None else ""
            lines.append(
                f"{emojis[kind]} <b>{labels[kind]}: {record.company_name}</b> "
                f"({record.ipo_type or 'N/A'}) — {record.price_band or 'N/A'}{gmp_line}"
            )
        return "\n\n".join(lines)

    def format_summary(self, records: List[IPORecord], title: str) -> str:
        if not records:
            return f"{title}\n\nNo active IPOs right now."

        lines = [title]
        for r in records:
            gmp_line = f"₹{r.current_gmp:g}" if r.current_gmp is not None else "N/A"
            lines.append(
                f"• <b>{r.company_name}</b> ({r.ipo_type or 'N/A'})\n"
                f"  {r.price_band or 'N/A'}{self._investment_line(r)} | GMP {gmp_line}{self._profit_line(r)}\n"
                f"  {r.open_date or 'TBA'}→{r.close_date or 'TBA'}"
            )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # High level send helpers -- one call, one message, covering every
    # affected IPO in the batch.
    # ------------------------------------------------------------------
    def notify_new_ipos(self, records: List[IPORecord]) -> bool:
        if not records:
            return True
        return self.send_message(self.format_new_ipos_batch(records))

    def notify_gmp_updates(
        self, events: List["tuple[IPORecord, Optional[float], Optional[float]]"]
    ) -> bool:
        if not events:
            return True
        return self.send_message(self.format_gmp_updates_batch(events))

    def notify_milestones(self, events: List["tuple[IPORecord, str]"]) -> bool:
        if not events:
            return True
        return self.send_message(self.format_milestones_batch(events))

    def notify_summary(self, records: List[IPORecord], title: str) -> bool:
        return self.send_message(self.format_summary(records, title))
