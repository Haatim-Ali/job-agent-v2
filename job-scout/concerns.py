"""Concerns about a posting or the bot itself, raised by reacting 💀 to an alert.

The bot opens one thread per posting in #concerns; every message typed there is stored here so the
open ones can be reviewed and fixed later (`python concerns.py` prints them). No API calls.
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db")


def conn(db_path=None):
    c = sqlite3.connect(db_path or DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS concern_threads (
            thread_id INTEGER PRIMARY KEY,
            job_url TEXT,
            title TEXT,
            company TEXT,
            source_link TEXT,
            status TEXT DEFAULT 'open',
            resolution TEXT,
            opened TEXT,
            closed TEXT
        );
        CREATE TABLE IF NOT EXISTS concern_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER,
            text TEXT,
            created TEXT
        );
    """)
    return c


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def thread_name(title, company):
    """Discord caps thread names at 100 characters."""
    return f"💀 {title} @ {company}"[:100]


def open_thread(thread_id, job_url, title, company, source_link, db_path=None):
    with conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO concern_threads (thread_id, job_url, title, company, source_link, opened) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (thread_id, job_url, title, company, source_link, _now()))


def open_thread_for(job_url, db_path=None):
    """The open concern thread for this posting, so a second 💀 reuses it instead of making another."""
    if not job_url:
        return None
    with conn(db_path) as c:
        row = c.execute("SELECT thread_id FROM concern_threads WHERE job_url = ? AND status = 'open' "
                        "ORDER BY opened DESC LIMIT 1", (job_url,)).fetchone()
    return row[0] if row else None


def is_tracked(thread_id, db_path=None):
    with conn(db_path) as c:
        return c.execute("SELECT 1 FROM concern_threads WHERE thread_id = ?", (thread_id,)).fetchone() is not None


def add_note(thread_id, text, db_path=None):
    with conn(db_path) as c:
        c.execute("INSERT INTO concern_notes (thread_id, text, created) VALUES (?, ?, ?)", (thread_id, text, _now()))
        c.execute("UPDATE concern_threads SET status = 'open', closed = NULL WHERE thread_id = ?", (thread_id,))


def resolve(thread_id, resolution="", db_path=None):
    """Mark a thread resolved; False if it isn't a concern thread."""
    with conn(db_path) as c:
        cur = c.execute("UPDATE concern_threads SET status = 'resolved', resolution = ?, closed = ? WHERE thread_id = ?",
                        (resolution, _now(), thread_id))
    return cur.rowcount > 0


def open_concerns(db_path=None):
    """[{thread_id, job_url, title, company, source_link, opened, notes: [text, ...]}], oldest first."""
    with conn(db_path) as c:
        threads = c.execute("SELECT thread_id, job_url, title, company, source_link, opened FROM concern_threads "
                            "WHERE status = 'open' ORDER BY opened").fetchall()
        out = []
        for tid, url, title, company, link, opened in threads:
            notes = [t for (t,) in c.execute("SELECT text FROM concern_notes WHERE thread_id = ? ORDER BY id", (tid,))]
            out.append({"thread_id": tid, "job_url": url, "title": title, "company": company,
                        "source_link": link, "opened": opened, "notes": notes})
    return out


def summary_lines(items):
    """One Discord line per open concern: a thread link, the posting and how many notes it has."""
    return [f"• <#{i['thread_id']}> **{i['title']}** at **{i['company']}** ({len(i['notes'])} note"
            f"{'' if len(i['notes']) == 1 else 's'})" for i in items]


if __name__ == "__main__":
    items = open_concerns(sys.argv[1] if len(sys.argv) > 1 else None)
    if not items:
        print("No open concerns.")
    for i in items:
        print(f"[{i['thread_id']}] {i['title']} @ {i['company']}  (opened {i['opened']})\n  {i['job_url']}")
        for n in i["notes"] or ["(no notes yet)"]:
            print(f"  - {n}")
        print()
