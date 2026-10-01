"""
Career-site sources: pull postings straight from company job boards instead of LinkedIn.

Greenhouse, Lever and Ashby expose public JSON job boards; Amazon has its own search JSON.
Each fetcher returns job dicts shaped like a JobSpy row so scraper.process_jobs() can use them.
"""

import logging
import re
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (job-scout)"}


def _html_to_text(html):
    if not html:
        return ""
    # Greenhouse double-escapes its content (&lt;p&gt;...), so unescape once before parsing.
    text = BeautifulSoup(html, "html.parser").get_text()
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(separator="\n")
    return text.strip()


def _parse_dt(value):
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: epoch ms
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:  # Amazon: "September 24, 2026"
        return datetime.strptime(value, "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _job(title, company, location, url, description, posted, site, board=""):
    return {
        "title": title,
        "company": company,
        "location": location,
        "job_url": url,
        "description": description,
        "date_posted": posted,
        "applicants": None,
        "site": site,
        "board": board,  # ATS board/company slug, used by apply_prep to find the application form
    }


def fetch_greenhouse(board):
    resp = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
        params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        company = j.get("company_name") or board.title()
        jobs.append(_job(
            j.get("title", ""), company, (j.get("location") or {}).get("name", ""),
            j.get("absolute_url", ""), _html_to_text(j.get("content")),
            _parse_dt(j.get("first_published") or j.get("updated_at")), "greenhouse", board,
        ))
    return jobs


def fetch_lever(company):
    resp = requests.get(
        f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"},
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for j in resp.json():
        desc = "\n".join(filter(None, [j.get("descriptionPlain"), j.get("additionalPlain")]))
        jobs.append(_job(
            j.get("text", ""), company.title(), (j.get("categories") or {}).get("location", ""),
            j.get("hostedUrl", ""), desc, _parse_dt(j.get("createdAt")), "lever", company,
        ))
    return jobs


def fetch_ashby(board):
    resp = requests.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{board}",
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        location = j.get("location") or ""
        if j.get("isRemote"):
            location = f"{location} (Remote)".strip()
        jobs.append(_job(
            j.get("title", ""), board.title(), location, j.get("jobUrl", ""),
            j.get("descriptionPlain") or _html_to_text(j.get("descriptionHtml")),
            _parse_dt(j.get("publishedAt")), "ashby", board,
        ))
    return jobs


def fetch_amazon(query):
    resp = requests.get(
        "https://www.amazon.jobs/en/search.json",
        params={"base_query": query, "result_limit": 50, "sort": "recent", "country": "USA"},
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        desc = "\n".join(filter(None, [
            j.get("description_short") or j.get("description"),
            "Basic qualifications:\n" + (j.get("basic_qualifications") or ""),
            "Preferred qualifications:\n" + (j.get("preferred_qualifications") or ""),
        ]))
        jobs.append(_job(
            j.get("title", ""), "Amazon", j.get("normalized_location", ""),
            "https://www.amazon.jobs" + j.get("job_path", ""), _html_to_text(desc),
            _parse_dt(j.get("posted_date")), "amazon",
        ))
    return jobs


def title_matches(title, role_keywords, intern_keywords, exclude_titles):
    t = f" {str(title).lower()} "
    if any(x in t for x in exclude_titles):
        return False
    # Whole-word match so "intern" doesn't hit "Internal", "International", "Internals"
    is_intern = any(re.search(rf"\b{re.escape(k)}s?\b", t) for k in intern_keywords)
    return is_intern and any(k in t for k in role_keywords)


def fetch_all(cfg, role_keywords, intern_keywords, exclude_titles):
    """Return recent, title-matching postings from every configured career site."""
    max_age = timedelta(hours=cfg.get("max_age_hours", 72))
    now = datetime.now(timezone.utc)

    sources = (
        [(fetch_greenhouse, b) for b in cfg.get("greenhouse", [])]
        + [(fetch_lever, c) for c in cfg.get("lever", [])]
        + [(fetch_ashby, b) for b in cfg.get("ashby", [])]
        + [(fetch_amazon, q) for q in cfg.get("amazon_queries", [])]
    )

    results, seen_urls = [], set()
    for fetch, arg in sources:
        try:
            jobs = fetch(arg)
        except Exception as e:
            logging.warning(f"Career site {fetch.__name__}({arg}) failed: {e}")
            continue
        kept = 0
        for job in jobs:
            if not job["job_url"] or job["job_url"] in seen_urls:
                continue
            if not title_matches(job["title"], role_keywords, intern_keywords, exclude_titles):
                continue
            posted = job["date_posted"]
            if posted is None or now - posted > max_age:
                continue
            seen_urls.add(job["job_url"])
            results.append(job)
            kept += 1
        logging.info(f"Career site {fetch.__name__}({arg}): {len(jobs)} postings, {kept} new-ish matches")
    return results
