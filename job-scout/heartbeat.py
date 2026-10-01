"""
Scraper heartbeat: proof that the 30-minute cron job is still running.

The scraper writes .last_run when a run finishes (and optionally pings healthchecks.io).
The always-on bot checks it every 15 minutes and posts one Discord warning if no run has
finished for STALE_AFTER_MIN minutes, then one "back to normal" message when runs resume.

healthchecks.io (optional, set HEALTHCHECK_URL in .env) covers the case this can't: the
whole server being down, which takes the bot down too.
"""

import json
import logging
import os
from datetime import datetime, timezone

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_PATH = os.path.join(SCRIPT_DIR, ".last_run")
STALE_AFTER_MIN = 75   # runs every 30 min and take ~12, so two missed runs


def _healthcheck(suffix=""):
    url = os.getenv("HEALTHCHECK_URL")
    if not url:
        return
    try:
        requests.get(url.rstrip("/") + suffix, timeout=10)
    except requests.RequestException as e:
        logging.warning(f"Healthcheck ping failed: {e}")


def run_started():
    _healthcheck("/start")


def run_finished(alerted, error=None, path=HEARTBEAT_PATH):
    """Record a finished run. A run that ended with an error still counts as a heartbeat
    (the job is alive), but healthchecks.io is told it failed."""
    data = {"finished": datetime.now(timezone.utc).isoformat(), "alerted": alerted, "error": error}
    with open(path, "w") as f:
        json.dump(data, f)
    _healthcheck("/fail" if error else "")


def read(path=HEARTBEAT_PATH):
    try:
        with open(path) as f:
            data = json.load(f)
        data["finished"] = datetime.fromisoformat(data["finished"])
        return data
    except (OSError, ValueError, KeyError):
        return None


def minutes_since_last_run(path=HEARTBEAT_PATH, now=None):
    """Minutes since the last finished run, or None if there's no heartbeat yet."""
    data = read(path)
    if not data:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - data["finished"]).total_seconds() / 60


def check(was_stale, path=HEARTBEAT_PATH, now=None):
    """Returns (is_stale, message_or_None). A message is returned only when the state
    changes, so the bot warns once and confirms recovery once."""
    minutes = minutes_since_last_run(path, now)
    if minutes is None:
        return was_stale, None   # never ran under this code yet: nothing to compare against
    stale = minutes > STALE_AFTER_MIN
    if stale and not was_stale:
        hours = minutes / 60
        return True, (f"⚠️ The job scraper hasn't finished a run in {hours:.1f} hours "
                      f"(it should run every 30 minutes). Check `crontab -l` and "
                      f"`tail /var/log/jobscraper.log` on the VPS.")
    if was_stale and not stale:
        return False, "✅ The job scraper is running again."
    return stale, None
