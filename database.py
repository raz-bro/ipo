"""SQLite persistence layer for the IPO bot.

Three tables:

- ``ipo``: one row per IPO, holding the latest known snapshot of its data.
- ``gmp_history``: append-only log of every GMP reading ever seen, used for
  change detection and the GMP history graph/CSV export.
- ``notifications_sent``: append-only log of every Telegram notification
  sent, used to avoid duplicate alerts.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator, List, Optional

from config import settings
from utils import logger, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS ipo (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ipo_name            TEXT NOT NULL UNIQUE,
    company_name        TEXT,
    ipo_type            TEXT,
    price_band          TEXT,
    lot_size            TEXT,
    issue_size          TEXT,
    open_date           TEXT,
    close_date          TEXT,
    listing_date        TEXT,
    allotment_date      TEXT,
    registrar           TEXT,
    exchange            TEXT,
    current_gmp         REAL,
    gmp_change          REAL,
    kostak               TEXT,
    subject_to_sauda    TEXT,
    source_url          TEXT,
    last_updated        TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmp_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ipo_id              INTEGER NOT NULL REFERENCES ipo(id) ON DELETE CASCADE,
    gmp                 REAL,
    kostak              TEXT,
    subject_to_sauda    TEXT,
    recorded_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications_sent (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ipo_id              INTEGER REFERENCES ipo(id) ON DELETE CASCADE,
    notification_type   TEXT NOT NULL,
    details             TEXT,
    sent_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gmp_history_ipo_id ON gmp_history(ipo_id);
CREATE INDEX IF NOT EXISTS idx_notifications_ipo_type
    ON notifications_sent(ipo_id, notification_type);
"""


@dataclass
class IPORecord:
    """A single IPO's current known state."""

    ipo_name: str
    company_name: str = ""
    ipo_type: str = ""
    price_band: str = ""
    lot_size: str = ""
    issue_size: str = ""
    open_date: Optional[str] = None
    close_date: Optional[str] = None
    listing_date: Optional[str] = None
    allotment_date: Optional[str] = None
    registrar: str = ""
    exchange: str = ""
    current_gmp: Optional[float] = None
    gmp_change: Optional[float] = None
    kostak: str = ""
    subject_to_sauda: str = ""
    source_url: str = ""
    last_updated: str = field(default_factory=now_iso)
    id: Optional[int] = None
    created_at: Optional[str] = None


class Database:
    """Thin synchronous wrapper around a single SQLite database file."""

    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        logger.info("Database ready at %s", self.db_path)

    # ------------------------------------------------------------------
    # IPO CRUD
    # ------------------------------------------------------------------
    def get_ipo_by_name(self, ipo_name: str) -> Optional[IPORecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ipo WHERE ipo_name = ?", (ipo_name,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_ipo_by_id(self, ipo_id: int) -> Optional[IPORecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ipo WHERE id = ?", (ipo_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_ipos(self) -> List[IPORecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM ipo ORDER BY created_at DESC").fetchall()
        return [self._row_to_record(r) for r in rows]

    # Fields compared to decide whether an existing IPO row actually changed.
    # last_updated/created_at/id are deliberately excluded -- they're not
    # "content", and touching last_updated on a no-op cycle would make the
    # database file (and the GitHub Actions commit that persists it) churn
    # every single cycle even when nothing about the IPO actually changed.
    _COMPARABLE_FIELDS = (
        "company_name", "ipo_type", "price_band", "lot_size", "issue_size",
        "open_date", "close_date", "listing_date", "allotment_date",
        "registrar", "exchange", "current_gmp", "gmp_change", "kostak",
        "subject_to_sauda", "source_url",
    )

    def upsert_ipo(self, record: IPORecord) -> IPORecord:
        """Insert a new IPO or update an existing one by ipo_name.

        If an existing row's content is identical to the incoming record,
        no UPDATE is executed at all (not even a no-op one) -- SQLite bumps
        internal file metadata on every write transaction regardless of
        whether any column value actually changed, so skipping the write
        entirely (rather than just skipping which columns get set) is what
        keeps the database file byte-identical across a no-change cycle.

        Returns the persisted record (with its id populated).
        """
        existing = self.get_ipo_by_name(record.ipo_name)
        with self._connect() as conn:
            if existing:
                changed = any(
                    getattr(existing, f) != getattr(record, f)
                    for f in self._COMPARABLE_FIELDS
                )
                if changed:
                    conn.execute(
                        """
                        UPDATE ipo SET
                            company_name = ?, ipo_type = ?, price_band = ?,
                            lot_size = ?, issue_size = ?, open_date = ?,
                            close_date = ?, listing_date = ?, allotment_date = ?,
                            registrar = ?, exchange = ?, current_gmp = ?,
                            gmp_change = ?, kostak = ?, subject_to_sauda = ?,
                            source_url = ?, last_updated = ?
                        WHERE ipo_name = ?
                        """,
                        (
                            record.company_name, record.ipo_type, record.price_band,
                            record.lot_size, record.issue_size, record.open_date,
                            record.close_date, record.listing_date, record.allotment_date,
                            record.registrar, record.exchange, record.current_gmp,
                            record.gmp_change, record.kostak, record.subject_to_sauda,
                            record.source_url, record.last_updated, record.ipo_name,
                        ),
                    )
                else:
                    record.last_updated = existing.last_updated
                record.id = existing.id
                record.created_at = existing.created_at
            else:
                cur = conn.execute(
                    """
                    INSERT INTO ipo (
                        ipo_name, company_name, ipo_type, price_band, lot_size,
                        issue_size, open_date, close_date, listing_date,
                        allotment_date, registrar, exchange, current_gmp,
                        gmp_change, kostak, subject_to_sauda, source_url,
                        last_updated, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.ipo_name, record.company_name, record.ipo_type,
                        record.price_band, record.lot_size, record.issue_size,
                        record.open_date, record.close_date, record.listing_date,
                        record.allotment_date, record.registrar, record.exchange,
                        record.current_gmp, record.gmp_change, record.kostak,
                        record.subject_to_sauda, record.source_url,
                        record.last_updated, now_iso(),
                    ),
                )
                record.id = cur.lastrowid
                record.created_at = now_iso()
        return record

    def update_gmp(self, ipo_id: int, new_gmp: Optional[float], gmp_change: Optional[float]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE ipo SET current_gmp = ?, gmp_change = ?, last_updated = ? WHERE id = ?",
                (new_gmp, gmp_change, now_iso(), ipo_id),
            )

    # ------------------------------------------------------------------
    # GMP history
    # ------------------------------------------------------------------
    def add_gmp_history(
        self,
        ipo_id: int,
        gmp: Optional[float],
        kostak: str = "",
        subject_to_sauda: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gmp_history (ipo_id, gmp, kostak, subject_to_sauda, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ipo_id, gmp, kostak, subject_to_sauda, now_iso()),
            )

    def get_latest_gmp_history(self, ipo_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM gmp_history WHERE ipo_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (ipo_id,),
            ).fetchone()

    def get_gmp_history(self, ipo_id: int) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM gmp_history WHERE ipo_id = ? ORDER BY id ASC",
                (ipo_id,),
            ).fetchall()

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def has_notification(self, ipo_id: Optional[int], notification_type: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM notifications_sent
                WHERE (ipo_id IS ? OR ipo_id = ?) AND notification_type = ?
                LIMIT 1
                """,
                (ipo_id, ipo_id, notification_type),
            ).fetchone()
        return row is not None

    def record_notification(
        self, ipo_id: Optional[int], notification_type: str, details: str = ""
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notifications_sent (ipo_id, notification_type, details, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (ipo_id, notification_type, details, now_iso()),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> IPORecord:
        data = dict(row)
        return IPORecord(**data)

    def as_dict_list(self) -> List[dict]:
        """All IPOs as plain dicts, e.g. for CSV export or a dashboard."""
        return [asdict(r) for r in self.list_ipos()]
