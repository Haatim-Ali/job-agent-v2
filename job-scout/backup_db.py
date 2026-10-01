#!/usr/bin/env python3
"""
Nightly backup of jobs.db (run from cron).

Uses SQLite's online backup API, which makes a consistent copy even while the scraper or
bot is writing (copying the file with cp mid-write can produce a corrupt backup). Copies are
gzipped into ~/backups/ and the newest KEEP are kept.

  python backup_db.py            # back up now
"""

import gzip
import os
import shutil
import sqlite3
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "jobs.db")
BACKUP_DIR = os.path.expanduser(os.getenv("BACKUP_DIR", "~/backups"))
KEEP = 14


def backup(db_path=DB_PATH, backup_dir=BACKUP_DIR, keep=KEEP, stamp=None):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y-%m-%d")
    raw = os.path.join(backup_dir, f"jobs-{stamp}.db")
    src, dst = sqlite3.connect(db_path), sqlite3.connect(raw)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    with open(raw, "rb") as f_in, gzip.open(raw + ".gz", "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw)

    backups = sorted(f for f in os.listdir(backup_dir) if f.startswith("jobs-") and f.endswith(".db.gz"))
    for old in backups[:-keep]:
        os.remove(os.path.join(backup_dir, old))
    return raw + ".gz"


if __name__ == "__main__":
    print(backup())
