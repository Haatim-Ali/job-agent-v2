"""
Career-site sources: pull postings straight from company job boards instead of LinkedIn.

Greenhouse, Lever, Ashby and SmartRecruiters expose public JSON job boards; Workday career
sites call a hidden JSON API; Amazon has its own search JSON; SimplifyJobs publishes the
community-curated Summer 2027 internship list as one JSON file.
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
        if isinstance(value, (int, float)):  # Lever: epoch ms; SimplifyJobs: epoch seconds
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
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


def fetch_smartrecruiters(company):
    """Public SmartRecruiters Posting API. company: the id in jobs.smartrecruiters.com/<id>/..."""
    base = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    resp = requests.get(base, params={"q": "intern", "limit": 100}, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("content", []):
        loc = j.get("location") or {}
        location = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country", "").upper()]))
        if loc.get("remote"):
            location = f"{location} (Remote)".strip()
        jobs.append(_job(
            j.get("name", ""), (j.get("company") or {}).get("name") or company, location,
            f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}", "",
            _parse_dt(j.get("releasedDate")), "smartrecruiters", company,
        ))
    return jobs


def smartrecruiters_description(company, posting_id):
    resp = requests.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}",
                        headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    sections = (resp.json().get("jobAd") or {}).get("sections") or {}
    return "\n\n".join(_html_to_text((sections.get(k) or {}).get("text"))
                        for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))


# Workday: "<tenant>|<wd host>|<site>", from a careers URL like
# https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite -> "nvidia|wd5|NVIDIAExternalCareerSite"
_WORKDAY_AGE = re.compile(r"(\d+)\+? Days? Ago", re.I)


def _workday_posted(text, now=None):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> datetime."""
    now = now or datetime.now(timezone.utc)
    text = str(text or "")
    if "today" in text.lower():
        return now
    if "yesterday" in text.lower():
        return now - timedelta(days=1)
    m = _WORKDAY_AGE.search(text)
    return now - timedelta(days=int(m.group(1))) if m else None


def fetch_workday(spec, pages=5, max_age_hours=72):
    """Workday's career-site JSON API (no key needed). Descriptions need a second request per
    job, so they're fetched only for fresh postings whose title matches the role filter
    (titles are filtered again in fetch_all)."""
    tenant, wd, site = spec.split("|")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    headers = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    jobs, cutoff = [], datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for page in range(pages):
        resp = requests.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": page * 20,
                                                    "searchText": "intern"}, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        postings = resp.json().get("jobPostings", [])
        for j in postings:
            posted = _workday_posted(j.get("postedOn"))
            if posted is None or posted < cutoff or not re.search(r"\bintern", j.get("title", ""), re.I):
                continue
            desc = ""
            try:
                info = requests.get(base + j["externalPath"], headers=headers, timeout=TIMEOUT).json()
                desc = _html_to_text((info.get("jobPostingInfo") or {}).get("jobDescription"))
            except (requests.RequestException, ValueError, KeyError):
                pass
            jobs.append(_job(
                j.get("title", ""), tenant.title(), j.get("locationsText", ""),
                f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{j.get('externalPath', '')}", desc,
                posted, "workday", spec,
            ))
        if len(postings) < 20:
            break
    return jobs


SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
GRAD_ONLY = {"Master's", "PhD", "MBA"}


def _ats_from_url(url):
    """(site, board) for a posting URL on a board this project understands, else ("other", "")."""
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/\d+", url)
    if m:
        return "greenhouse", m.group(1)
    m = re.search(r"jobs\.lever\.co/([\w.-]+)/", url)
    if m:
        return "lever", m.group(1)
    m = re.search(r"jobs\.ashbyhq\.com/([\w.-]+)/", url)
    if m:
        return "ashby", m.group(1)
    m = re.search(r"jobs\.smartrecruiters\.com/([\w-]+)/", url)
    if m:
        return "smartrecruiters", m.group(1)
    m = re.search(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)/", url)
    if m:
        return "workday", "|".join(m.groups())
    return "other", ""


def fetch_simplify(url=SIMPLIFY_URL):
    """SimplifyJobs' Summer 2027 internship list: community submissions plus Simplify's hourly
    scrape of company career pages. Only open, visible listings that accept undergraduates.
    The list has no descriptions; Claude scores on title, company, location and the
    listing's metadata, and packets read the full description from the ATS later."""
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    jobs = []
    for j in resp.json():
        if not j.get("active") or not j.get("is_visible", True):
            continue
        degrees = set(j.get("degrees") or [])
        if degrees and degrees <= GRAD_ONLY:
            continue
        site, board = _ats_from_url(j.get("url", ""))
        meta = [f"Terms: {', '.join(j.get('terms') or [])}", f"Category: {j.get('category', '')}",
                f"Degrees: {', '.join(sorted(degrees)) or 'not listed'}",
                f"Sponsorship: {j.get('sponsorship', 'not listed')}",
                "(Listing from the SimplifyJobs Summer 2027 internship list; no full description.)"]
        job = _job(
            j.get("title", ""), j.get("company_name", ""), "; ".join(j.get("locations") or []),
            j.get("url", ""), "\n".join(meta), _parse_dt(j.get("date_posted")),
            site if site != "other" else "simplify", board,
        )
        job["from_simplify"] = True
        jobs.append(job)
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
        + [(fetch_smartrecruiters, c) for c in cfg.get("smartrecruiters", [])]
        + [(fetch_workday, w) for w in cfg.get("workday", [])]
        + [(fetch_amazon, q) for q in cfg.get("amazon_queries", [])]
        + ([(lambda _: fetch_simplify(), "simplify")] if cfg.get("simplify", True) else [])
    )

    results, seen_urls = [], set()
    for fetch, arg in sources:
        try:
            jobs = fetch(arg)
        except Exception as e:
            logging.warning(f"Career site {getattr(fetch, '__name__', 'source')}({arg}) failed: {e}")
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
        logging.info(f"Career site {getattr(fetch, '__name__', 'source')}({arg}): {len(jobs)} postings, {kept} new-ish matches")
    return results
