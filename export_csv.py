"""Bonus: export the IPO table and GMP history to CSV files.

Usage:
    python export_csv.py            # export everything
    python export_csv.py --ipo-name "ABC Limited"   # only one IPO's GMP history

Writes into the directory configured by CSV_EXPORT_DIR (default: exports/).
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from config import settings
from database import Database
from utils import logger


def export_ipos(db: Database) -> str:
    settings.csv_export_dir.mkdir(parents=True, exist_ok=True)
    path = settings.csv_export_dir / "ipos.csv"
    df = pd.DataFrame(db.as_dict_list())
    df.to_csv(path, index=False)
    logger.info("Exported %d IPO row(s) to %s", len(df), path)
    return str(path)


def export_gmp_history(db: Database, ipo_name: str | None = None) -> str:
    settings.csv_export_dir.mkdir(parents=True, exist_ok=True)
    rows = []

    ipos = [db.get_ipo_by_name(ipo_name)] if ipo_name else db.list_ipos()
    ipos = [i for i in ipos if i is not None]

    for ipo in ipos:
        for row in db.get_gmp_history(ipo.id):
            record = dict(row)
            record["ipo_name"] = ipo.ipo_name
            rows.append(record)

    filename = f"gmp_history_{ipo_name}.csv" if ipo_name else "gmp_history.csv"
    path = settings.csv_export_dir / filename.replace(" ", "_")
    pd.DataFrame(rows).to_csv(path, index=False)
    logger.info("Exported %d GMP history row(s) to %s", len(rows), path)
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export IPO bot data to CSV")
    parser.add_argument(
        "--ipo-name", default=None, help="Only export GMP history for this IPO name"
    )
    args = parser.parse_args()

    db = Database()
    ipos_path = export_ipos(db)
    history_path = export_gmp_history(db, args.ipo_name)

    print(f"IPOs exported to:        {ipos_path}")
    print(f"GMP history exported to: {history_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
