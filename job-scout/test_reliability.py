"""
Tests for heartbeat.py (scraper watchdog) and backup_db.py.
Run with: python -m pytest job-scout/test_reliability.py -v
"""

import gzip
import sqlite3
from datetime import datetime, timedelta, timezone

import backup_db
import heartbeat


def test_no_heartbeat_means_no_alert(tmp_path):
    assert heartbeat.check(False, path=str(tmp_path / "missing")) == (False, None)


def test_warns_once_then_confirms_recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    path = str(tmp_path / ".last_run")
    heartbeat.run_finished(3, path=path)
    now = datetime.now(timezone.utc)

    assert heartbeat.check(False, path, now + timedelta(minutes=30)) == (False, None)
    stale, msg = heartbeat.check(False, path, now + timedelta(minutes=120))
    assert stale and "2.0 hours" in msg
    assert heartbeat.check(True, path, now + timedelta(minutes=180)) == (True, None)   # no repeat

    heartbeat.run_finished(0, path=path)
    stale, msg = heartbeat.check(True, path)
    assert not stale and "running again" in msg


def test_failed_run_still_counts_and_pings_fail(tmp_path, monkeypatch):
    pings = []
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc/")
    monkeypatch.setattr(heartbeat.requests, "get", lambda url, **kw: pings.append(url))
    path = str(tmp_path / ".last_run")
    heartbeat.run_started()
    heartbeat.run_finished(0, error="Claude unavailable", path=path)
    assert pings == ["https://hc-ping.com/abc/start", "https://hc-ping.com/abc/fail"]
    assert heartbeat.read(path)["error"] == "Claude unavailable"
    assert heartbeat.minutes_since_last_run(path) < 1


def test_backup_is_consistent_gzip_and_pruned(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE seen_jobs (job_url TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO seen_jobs VALUES ('https://x')")
    conn.commit()
    out = tmp_path / "backups"
    for day in range(1, 5):
        path = backup_db.backup(str(db), str(out), keep=2, stamp=f"2026-10-0{day}")
    assert sorted(p.name for p in out.iterdir()) == ["jobs-2026-10-03.db.gz", "jobs-2026-10-04.db.gz"]
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.open(path).read())
    assert sqlite3.connect(restored).execute("SELECT job_url FROM seen_jobs").fetchall() == [("https://x",)]
