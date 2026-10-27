#!/usr/bin/env python3
"""
Job Scout Scraper - runs every 30 min via cron
Uses JobSpy for multi-board scraping with Claude relevance filtering
"""

import os
import sqlite3
import json
import logging
import re
import subprocess
import sys
import time
import requests
import yaml
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from jobspy import scrape_jobs
from discord_webhook import DiscordWebhook, DiscordEmbed
import anthropic

import career_sites
from claude_json import SCORE_MODEL, ask_json, obj, BOOL, INT, STR
import apply_prep
import heartbeat

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db")
MEMORY_REPO_PATH = os.getenv("MEMORY_REPO_PATH")  # local clone of job-agent-memory repo

# Hard filter: job descriptions mentioning 5+ years experience.
# Runs on the FULL description after fetch_full_job_details(), before Claude.
EXPERIENCE_FILTER_RE = re.compile(
    r'(?<![0-9-])([5-9]|\d{2})\+?\s*(?:to\s*\d+\s*)?years?\s*(?:of\s*)?(?:experience|exp\.?|required|minimum|min\.?)\b'
    r'|minimum\s+(?:of\s+)?([5-9]|\d{2})\s+years?'
    r'|(?<![0-9-])([5-9]|\d{2})\s*\+\s*(?:years?|yrs?)\b',
    re.IGNORECASE
)

logging.basicConfig(
    filename="/var/log/jobscraper.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %I:%M:%S %p"
)

# Load config from scraper_config.json — edit that file to tune search terms, locations, etc.
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_config.json")
with open(_CONFIG_PATH) as _f:
    _cfg = json.load(_f)

# Load candidate profile — edit candidate_profile.yaml to personalize the AI filters
_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candidate_profile.yaml")
with open(_PROFILE_PATH) as _pf:
    _profile = yaml.safe_load(_pf)

SEARCH_TERMS = _cfg["search_terms"]
LOCATIONS = _cfg["locations"]
WATCHLIST_COMPANIES = _cfg["watchlist_companies"]
EXCLUDE_INDUSTRIES = _cfg["exclude_industries"]
EXCLUDE_COMPANIES = [c.lower() for c in _cfg.get("exclude_companies", [])]
CAREER_SITES = _cfg.get("career_sites", {})
# Companies where every internship is alerted, whatever the role or fit score (e.g. Sephora)
WATCH_INTERNSHIPS = _cfg.get("watch_internships", [])
ROLE_KEYWORDS = _cfg.get("role_keywords", [])
INTERN_KEYWORDS = _cfg.get("intern_keywords", [])
CAREER_SITE_EXCLUDE_TITLES = _cfg.get("career_site_exclude_titles", [])
REPOST_SIGNALS = _cfg["repost_signals"]
# Off since 2026-10-26: #job-alerts holds alerts only, and tailoring runs only when Haatim reacts 👍
# (results go to the actions channel). Set true to tailor every alert automatically (tailor_alerts).
TAILOR_EVERY_ALERT = _cfg.get("tailor_every_alert", False)
MAX_TAILORED_PER_RUN = _cfg.get("max_tailored_per_run", 15)
# Application packets (form answers for autofilling) are off since 2026-10-04: Haatim fills
# forms with each site's own autofill and the tailored resume. The code is kept.
APPLICATION_PACKETS = _cfg.get("application_packets", False)
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
_alerted_jobs = []   # jobs alerted this run, tailored at the end of the run


def init_db():
    conn = sqlite3.connect(DB_PATH)
    # WAL lets the bot read and write while a scraper run is writing (and vice versa);
    # the setting is stored in the database file, so this only changes it once
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            date_found TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            job_url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            status TEXT DEFAULT 'applied',
            date_added TEXT,
            notes TEXT
        )
    """)
    conn.commit()
    return conn


def is_seen(conn, job_url):
    return conn.execute("SELECT 1 FROM seen_jobs WHERE job_url = ?", (job_url,)).fetchone() is not None


def mark_seen(conn, job_url, title="", company=""):
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs (job_url, title, company, date_found) VALUES (?, ?, ?, ?)",
        (job_url, str(title), str(company), datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def fetch_full_job_details(job_url):
    """Fetch full description and applicant count from the actual job page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(job_url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        full_desc = None
        for selector in [
            ".show-more-less-html__markup",
            ".description__text",
            ".job-description",
            "[data-testid='job-description']",
            ".jobsearch-jobDescriptionText",
        ]:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 200:
                full_desc = el.get_text(separator="\n", strip=True)
                break

        applicant_count = None
        page_text = soup.get_text()
        for pattern in [
            r"(\d[\d,]+)\s+applicants?",
            r"Over\s+(\d[\d,]+)\s+applicants?",
            r"Be among the first\s+(\d+)",
            r"(\d+)\s+people\s+clicked\s+apply",
            r"(\d+)\s+clicked\s+apply",
        ]:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                try:
                    applicant_count = int(match.group(1).replace(",", ""))
                except ValueError:
                    pass
                break

        # LinkedIn embeds "Reposted X minutes/hours/days ago" in the page HTML for reposts.
        # Match the specific pattern rather than any occurrence of "reposted" to avoid false positives.
        page_repost = bool(re.search(r"reposted\s+\d+\s+(minute|hour|day|week)s?\s+ago", resp.text, re.IGNORECASE))

        return full_desc, applicant_count, page_repost
    except Exception as e:
        logging.warning(f"Failed to fetch job details from {job_url}: {e}")
        return None, None, False


def quick_filter(job):
    title = str(job.get("title") or "").lower()
    company = str(job.get("company") or "").lower()
    title_company = title + " " + company

    if any(term in title_company for term in EXCLUDE_INDUSTRIES):
        return False, "excluded industry"

    if any(term in company for term in EXCLUDE_COMPANIES):
        return False, "excluded company"

    # JobSpy reports pay per its "interval"; internships are usually hourly,
    # so only compare against the matching floor.
    max_sal = job.get("max_amount")
    interval = str(job.get("interval") or "").lower()
    floors = {"yearly": 40000, "hourly": 25}
    if max_sal and interval in floors:
        try:
            if float(max_sal) < floors[interval]:
                return False, f"pay too low: {max_sal}/{interval}"
        except (ValueError, TypeError):
            pass

    return True, "ok"


def detect_repost(job):
    """Return True if the job shows signs of being a repost.
    Reposts get a fresh timestamp but are stale -- don't treat them as newly listed.
    """
    title = str(job.get("title") or "").lower()
    desc = str(job.get("description") or "").lower()[:600]
    combined = title + " " + desc
    return any(signal in combined for signal in REPOST_SIGNALS)


def passes_or_filter(job, is_repost=False):
    """Pass if posted < 2 hours ago OR applicants < 10.
    Reposts are never treated as fresh regardless of their timestamp.
    Returns (passes, age_hours, applicant_count).
    """
    applicant_count = job.get("applicants")
    age_hours = None

    date_posted = job.get("date_posted")
    if date_posted:
        try:
            now = datetime.now(timezone.utc)
            dp = date_posted
            if hasattr(dp, "tzinfo") and dp.tzinfo is None:
                dp = dp.replace(tzinfo=timezone.utc)
            age_hours = (now - dp).total_seconds() / 3600
        except Exception:
            pass

    # Reposts get a fresh timestamp but are stale -- ignore age for them
    age_under_2hrs = not is_repost and age_hours is not None and age_hours <= 2
    few_applicants = applicant_count is not None and applicant_count < 10

    if age_under_2hrs or few_applicants:
        return True, age_hours, applicant_count

    # Job is old (or reposted) -- require confirmed low applicant count to proceed.
    # Unknown applicant count on a non-fresh job is assumed stale.
    if age_hours is not None and age_hours > 2:
        return False, age_hours, applicant_count

    # Age truly unknown -- let Claude decide, but never give reposts a free pass
    if is_repost:
        return False, age_hours, applicant_count
    return True, age_hours, applicant_count


_SEASONS = {"summer", "fall", "autumn", "winter", "spring"}
_TITLE_FILLER = {"intern", "interns", "internship", "internships",
                 "co", "op", "coop", "the", "a", "an", "of", "and", "for", "at", "to", "in"}


def title_key(title):
    """(core words, seasons) of a title, order-free, with years, "intern" and punctuation
    dropped. "2027 Summer Intern – Machine Learning Engineer, AV/AI Platform" and "Machine
    Learning Engineer Intern - AV/AI Platform" share a core; seasons are kept apart so a
    Spring and a Summer program at one company stay two jobs."""
    words = [w for w in re.findall(r"[a-z0-9]+", str(title).lower()) if not re.fullmatch(r"20\d\d", w)]
    seasons = frozenset(w for w in words if w in _SEASONS)
    core = frozenset(w for w in words if w not in _TITLE_FILLER and w not in _SEASONS)
    return core, seasons


def same_role(title_a, title_b):
    (core_a, sea_a), (core_b, sea_b) = title_key(title_a), title_key(title_b)
    return bool(core_a) and core_a == core_b and (not sea_a or not sea_b or sea_a == sea_b)


def _company_key(company):
    return " ".join(re.findall(r"[a-z0-9]+", str(company).lower()))


def is_repost_by_title(conn, title, company):
    """True if the same role at the same company was seen in the last 7 days, under any wording
    (catches URL-refreshed reposts, and the same job arriving from LinkedIn and a company board
    or the Simplify list with differently worded titles)."""
    ckey = _company_key(company)
    if not title_key(title)[0] or not ckey:
        return False
    first = ckey.split()[0]
    rows = conn.execute(
        """SELECT title, company FROM seen_jobs
           WHERE lower(company) LIKE ? AND date_found > datetime('now', '-7 days')""",
        (f"%{first}%",),
    ).fetchall()
    return any(_company_key(c) == ckey and same_role(title, t) for t, c in rows)


def get_rejection_context():
    """Pull recent rejection reasons from DB to inform Claude."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT title, company, reason FROM rejections
               WHERE reason IS NOT NULL AND reason != ''
               ORDER BY date_rejected DESC LIMIT 15"""
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["You've recently passed on these roles and here's why (use this to calibrate):"]
        for title, company, reason in rows:
            lines.append(f"  - {title} @ {company}: \"{reason}\"")
        return "\n".join(lines)
    except Exception:
        return ""


def get_vault_rejection_context():
    """Read recent rows from job-agent-memory rejection-patterns.md to supplement SQLite context."""
    if not MEMORY_REPO_PATH:
        return ""
    md_path = os.path.join(MEMORY_REPO_PATH, "rejection-patterns.md")
    if not os.path.exists(md_path):
        return ""
    try:
        with open(md_path, encoding="utf-8") as f:
            lines = f.readlines()
        # Parse markdown table rows (skip header lines starting with | Date | or |---)
        rows = [
            l.strip() for l in lines
            if l.startswith("|") and not l.startswith("| Date") and not l.startswith("|---")
        ]
        if not rows:
            return ""
        recent = rows[-20:]  # last 20 dismissals
        parsed = []
        for row in recent:
            parts = [p.strip() for p in row.strip("|").split("|")]
            if len(parts) >= 4:
                _, title, company, reason = parts[0], parts[1], parts[2], parts[3]
                if reason:
                    parsed.append(f"  - {title} @ {company}: \"{reason}\"")
        if not parsed:
            return ""
        return "Additional dismissals from memory vault:\n" + "\n".join(parsed)
    except Exception:
        return ""


# Set when Claude is unreachable (e.g. out of API credits) so the rest of the run
# skips scoring instead of making calls that are certain to fail.
_claude_unavailable = None
API_ALERT_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_api_alert")


def notify_api_problem(error):
    """Post one Discord warning per day when Claude scoring is failing."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        if os.path.exists(API_ALERT_MARKER) and open(API_ALERT_MARKER).read().strip() == today:
            return
        mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else "@here"
        DiscordWebhook(
            url=WEBHOOK_URL,
            content=(f"{mention} ⚠️ Job Scout can't reach Claude, so no jobs are being scored or alerted. "
                     f"Unscored jobs are kept and retried once it works again.\nError: {str(error)[:300]}"),
        ).execute()
        with open(API_ALERT_MARKER, "w") as f:
            f.write(today)
    except Exception as e:
        logging.warning(f"Could not send API problem alert: {e}")


SCORE_SCHEMA = obj({"relevant": BOOL, "reason": STR, "fit_score": INT,
                    "experience_required": STR, "location_ok": BOOL})


def claude_relevance_check(job):
    global _claude_unavailable
    if _claude_unavailable:
        return {"relevant": False, "reason": "api error", "fit_score": 0, "api_error": True}
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    rejection_context = get_rejection_context()
    vault_context = get_vault_rejection_context()
    combined_context = "\n".join(filter(None, [rejection_context, vault_context]))

    p = _profile["main_roles"]
    disq = "\n".join(f"- {d}" for d in p["disqualifiers"])
    prompt = f"""You are filtering job postings for {_profile["name"]}, a {_profile["background"]}.

Target profile:
- Roles: {p["target_roles"]}
- Industry: {p["industry"]}
- Location: {p["locations"]}
- Pay: {p["min_pay"]}
- Experience: {p["experience_level"]}
- Internships: {p["include_internships"]}
- Exclude: {disq}

{combined_context}

Job posting:
Title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Description (first 3500 chars): {str(job.get("description") or "")[:3500]}

Answer with: relevant (true/false), reason (one sentence max), fit_score (1 to 10),
experience_required (e.g. "0-1 years" or "5+ years"), location_ok (true/false)."""

    try:
        result = ask_json(client, prompt, SCORE_SCHEMA, model=SCORE_MODEL, max_tokens=1000, label="score")
        result["fit_score"] = max(1, min(10, int(result["fit_score"])))   # schemas can't enforce ranges
        return result
    except Exception as e:
        logging.warning(f"Claude check failed: {e}")
        # Account-level problems (no credits, bad key) will fail every call: stop trying this run
        account_problem = isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)) \
            or "credit balance" in str(e).lower()
        if account_problem:
            _claude_unavailable = e
            notify_api_problem(e)
        # Temporary outages are worth retrying next run too; anything else (e.g. unparseable
        # output for one job) is treated as a rejection so it can't be retried forever.
        transient = isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError,
                                   anthropic.InternalServerError))
        return {"relevant": False, "reason": "api error", "fit_score": 0,
                "api_error": account_problem or transient}


def send_discord_alert(job, analysis, is_repost=False):
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else "@here"

    applicants = job.get("applicants")
    date_posted = job.get("date_posted")
    company_lower = str(job.get("company") or "").lower()
    is_watchlist = any(w in company_lower for w in WATCHLIST_COMPANIES)

    age_hours = None
    if date_posted:
        try:
            now = datetime.now(timezone.utc)
            dp = date_posted
            if hasattr(dp, "tzinfo") and dp.tzinfo is None:
                dp = dp.replace(tzinfo=timezone.utc)
            age_hours = (now - dp).total_seconds() / 3600
        except Exception:
            pass

    is_hot = not is_repost and age_hours is not None and age_hours <= 1

    if is_watchlist:
        color = "FFD700"
    elif is_hot:
        color = "00FF7F"
    else:
        color = "03b2f8"

    flags = []
    if is_repost:
        flags.append("♻️ REPOST -- timestamp unreliable, likely stale")
    elif is_hot:
        flags.append("🔥 FRESH -- under 1 hour old")
    elif age_hours is not None and age_hours <= 2:
        flags.append(f"🔥 FRESH -- posted {age_hours:.0f}h ago")
    if is_watchlist:
        flags.append("⭐ WATCHLIST COMPANY")
    if job.get("watched"):
        flags.append("👀 WATCHED COMPANY INTERNSHIP")
    if job.get("from_simplify"):
        flags.append("📋 FROM SIMPLIFY INTERNSHIP LIST")
    elif job.get("site") in ("greenhouse", "lever", "ashby", "amazon", "smartrecruiters", "workday", "successfactors"):
        flags.append("🏢 FROM COMPANY CAREER SITE")
    if applicants is not None:
        try:
            n = int(applicants)
            flags.append(f"👀 {n} applicant{'s' if n != 1 else ''}")
        except (ValueError, TypeError):
            pass

    posted_str = f"{age_hours:.1f}h ago" if age_hours is not None else str(date_posted) if date_posted else "Unknown"
    fit_score = analysis.get("fit_score", "?")
    description_preview = str(job.get("description") or "")[:300] + "..."

    embed_description = ("\n".join(flags) + "\n\n" if flags else "") + description_preview

    webhook = DiscordWebhook(
        url=WEBHOOK_URL,
        content=f"{mention} New job match! Fit score: {fit_score}/10"
    )

    embed = DiscordEmbed(
        title=f"{job.get('title', 'Unknown Role')} @ {job.get('company', 'Unknown')}",
        url=job.get("job_url", ""),
        description=embed_description,
        color=color
    )

    embed.add_embed_field(name="Company", value=str(job.get("company", "N/A")), inline=True)
    embed.add_embed_field(name="Location", value=str(job.get("location", "N/A")), inline=True)
    embed.add_embed_field(name="Posted", value=posted_str, inline=True)
    embed.add_embed_field(name="Applicants", value=str(applicants) if applicants is not None else "N/A", inline=True)
    embed.add_embed_field(name="Source", value=str(job.get("site", "N/A")).capitalize(), inline=True)
    embed.add_embed_field(name="Why it fits", value=str(analysis.get("reason", "N/A")), inline=False)
    embed.set_footer(text="👍 tailor resume  |  ✅ mark applied  |  ❌ dismiss")

    webhook.add_embed(embed)
    webhook.execute()


def process_jobs(jobs, conn, is_remote=False, from_career_site=False):
    """Filter, score and alert on an iterable of job dicts (JobSpy rows or career-site postings).

    Career-site postings are already age-filtered and have no LinkedIn page to scrape,
    so they skip the page fetch and the freshness/applicant OR filter.
    """
    alerted = 0
    for job_dict in jobs:
        job_url = str(job_dict.get("job_url", ""))
        if not job_url or is_seen(conn, job_url):
            continue

        passed, reason = quick_filter(job_dict)
        if not passed:
            logging.info(f"Quick filtered: {job_dict.get('title')} @ {job_dict.get('company')} -- {reason}")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        page_repost = False
        if not from_career_site:
            # Fetch full description, applicant count, and repost flag from page HTML
            full_desc, applicant_count, page_repost = fetch_full_job_details(job_url)
            if full_desc:
                job_dict["description"] = full_desc
            if applicant_count is not None:
                job_dict["applicants"] = applicant_count

        # Hard experience filter: reject before Claude if full description contains 5+ years requirement
        desc_text = str(job_dict.get("description") or "")
        if desc_text and EXPERIENCE_FILTER_RE.search(desc_text):
            logging.info(f"Experience filtered: {job_dict.get('title')} @ {job_dict.get('company')} -- 5+ years requirement found")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        title_repost = is_repost_by_title(conn, job_dict.get("title", ""), job_dict.get("company", ""))
        repost = page_repost or detect_repost(job_dict)
        if repost:
            logging.info(f"Repost detected: {job_dict.get('title')} @ {job_dict.get('company')} -- skipping freshness fast-pass")
        elif title_repost:
            # Same role already seen under another URL (e.g. LinkedIn and the career site) -- don't alert twice
            logging.info(f"Duplicate title+company within 7 days: {job_dict.get('title')} @ {job_dict.get('company')}")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        if not from_career_site:
            passes, age_hours, applicant_count = passes_or_filter(job_dict, is_repost=repost)
            if not passes:
                logging.info(f"OR filtered ({age_hours:.1f}h old, {applicant_count} applicants): {job_dict.get('title')} @ {job_dict.get('company')}")
                mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
                continue

        analysis = claude_relevance_check(job_dict)
        if analysis.get("api_error"):
            # Not a real rejection: leave it unseen so the next run scores it
            logging.info(f"Not scored (Claude unavailable), will retry: {job_dict.get('title')} @ {job_dict.get('company')}")
            continue

        min_score = 5
        if job_dict.get("watched"):
            pass   # watched-company internship: always alerted; the score is just shown
        elif not analysis.get("relevant") or analysis.get("fit_score", 0) < min_score:
            logging.info(f"Claude filtered: {job_dict.get('title')} @ {job_dict.get('company')} -- {analysis.get('reason')}")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        send_discord_alert(job_dict, analysis, is_repost=repost)
        _alerted_jobs.append(job_dict)
        if from_career_site:
            if APPLICATION_PACKETS and apply_prep.enqueue(conn, job_dict, analysis.get("fit_score")):
                logging.info(f"Queued for application packet: {job_dict.get('title')} @ {job_dict.get('company')}")
            if apply_prep.learn_board(conn, job_dict, "simplify" if job_dict.get("from_simplify") else "career site"):
                logging.info(f"Learned job board {job_dict.get('site')}/{job_dict.get('board')}")
        elif APPLICATION_PACKETS:
            # LinkedIn hides the application link; look for the same role on the company's own board
            try:
                ats_job = apply_prep.find_on_ats(job_dict.get("company"), job_dict.get("title"),
                                                 job_dict.get("description"), job_dict.get("location"))
                if ats_job and apply_prep.enqueue(conn, ats_job, analysis.get("fit_score")):
                    logging.info(f"Queued for application packet via {ats_job['site']}: "
                                 f"{job_dict.get('title')} @ {job_dict.get('company')} -> {ats_job['job_url']}")
                if ats_job and apply_prep.learn_board(conn, ats_job, "linkedin lookup"):
                    logging.info(f"Learned job board {ats_job['site']}/{ats_job['board']}")
            except Exception as e:
                logging.warning(f"ATS lookup failed for {job_dict.get('company')}: {e}")
        mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
        alerted += 1
        logging.info(f"Alerted: {job_dict.get('title')} @ {job_dict.get('company')} (score {analysis.get('fit_score')})")

    return alerted


def tailor_alerts(jobs, runner=subprocess.run):
    """Tailored resume + cover letter for each alerted job, posted to #job-alerts by tailor.py
    right under the alert. Runs after scraping so alerts go out first; one job at a time, each
    in its own process (tailor.py configures its own logging and Discord client).
    Descriptions under 400 characters (e.g. SimplifyJobs listings, which have none) are left
    for tailor.py to fetch from the posting itself."""
    import tempfile
    tailor_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tailor.py")
    done = 0
    for job in jobs:
        cmd = [sys.executable, tailor_py, job["job_url"], str(CHANNEL_ID)]
        desc = str(job.get("description") or "")
        jd_path = None
        if len(desc) >= 400:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(desc)
                jd_path = f.name
            cmd += ["--jd-file", jd_path]
        try:
            result = runner(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                done += 1
            else:
                logging.error(f"Tailoring failed for {job['job_url']}: {result.stderr[-300:]}")
        except Exception as e:
            logging.error(f"Tailoring failed for {job['job_url']}: {e}")
        finally:
            if jd_path and os.path.exists(jd_path):
                os.remove(jd_path)
    if jobs:
        logging.info(f"Tailored {done}/{len(jobs)} alerted job(s)")
    return done


def run():
    heartbeat.run_started()
    try:
        alerted = _run()
    except Exception as e:
        logging.exception("Scraper run crashed")
        heartbeat.run_finished(0, error=f"crashed: {e}")
        raise
    heartbeat.run_finished(alerted, error=f"Claude unavailable: {_claude_unavailable}" if _claude_unavailable else None)


def _run():
    logging.info("=== Scraper run started ===")
    conn = init_db()
    apply_prep.db_conn().close()  # make sure the apply_queue table exists
    total_alerted = 0

    # Company career sites first -- they often list roles before LinkedIn does
    try:
        sites = apply_prep.with_learned_boards(CAREER_SITES, conn)
        postings = career_sites.fetch_all(sites, ROLE_KEYWORDS, INTERN_KEYWORDS, CAREER_SITE_EXCLUDE_TITLES,
                                          watch=WATCH_INTERNSHIPS)
        total_alerted += process_jobs(postings, conn, from_career_site=True)
    except Exception as e:
        logging.error(f"Career sites step failed: {e}")

    for search_term in SEARCH_TERMS:
        for location in LOCATIONS:
            is_remote = location.strip().lower() == "remote"
            try:
                jobs_df = scrape_jobs(
                    site_name=["linkedin"],
                    search_term=search_term,
                    location="United States" if is_remote else location,
                    results_wanted=40,
                    hours_old=24,
                    job_type="internship",
                    is_remote=is_remote,
                )
                if jobs_df is not None and not jobs_df.empty:
                    logging.info(f"'{search_term}' in {location}: {len(jobs_df)} results")
                    rows = (row.to_dict() for _, row in jobs_df.iterrows())
                    total_alerted += process_jobs(rows, conn, is_remote=is_remote)
            except Exception as e:
                logging.error(f"Error scraping '{search_term}' in {location}: {e}")
            time.sleep(2)

    conn.close()

    if TAILOR_EVERY_ALERT and not _claude_unavailable:
        tailor_alerts(_alerted_jobs[:MAX_TAILORED_PER_RUN])

    # Build application packets for a few queued jobs (separate process: tailoring is slow
    # and configures its own logging)
    if APPLICATION_PACKETS and not _claude_unavailable:
        try:
            subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply_prep.py"),
                 "--pending", os.getenv("APPLY_PREP_PER_RUN", "3")],
                timeout=1200,
            )
        except Exception as e:
            logging.error(f"Application packet step failed: {e}")

    if _claude_unavailable:
        logging.error(f"Claude unavailable this run ({_claude_unavailable}); unscored jobs left for retry")
    logging.info(f"=== Scraper run complete. Jobs alerted: {total_alerted} ===")
    return total_alerted


if __name__ == "__main__":
    run()
