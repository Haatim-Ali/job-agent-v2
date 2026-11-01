"""
Career-site sources: pull postings straight from company job boards instead of LinkedIn.

Greenhouse, Lever, Ashby and SmartRecruiters expose public JSON job boards; Workday and
Eightfold career sites call hidden JSON APIs; Amazon has its own search JSON; SimplifyJobs publishes the
community-curated Summer 2027 internship list as one JSON file.
Each fetcher returns job dicts shaped like a JobSpy row so scraper.process_jobs() can use them.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
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
        pass
    try:  # RSS: "Tue, 06 Oct 2026 2:00:00 GMT"
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
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


_MAYBE_INTERN = re.compile(r"\b(intern|interns|internship|co-?op|student)\b", re.I)


def fetch_greenhouse(board, max_age_hours=72):
    """The board's job list without descriptions (SpaceX's and Anduril's are 30-45 MB with them),
    then one request per fresh internship-looking posting for its description. Titles are
    filtered properly in fetch_all; postings skipped here would be dropped there anyway."""
    base = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    resp = requests.get(base, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs, cutoff = [], datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for j in resp.json().get("jobs", []):
        posted = _parse_dt(j.get("first_published") or j.get("updated_at"))
        if posted is None or posted < cutoff or not _MAYBE_INTERN.search(j.get("title", "")):
            continue
        desc = ""
        try:
            detail = requests.get(f"{base}/{j['id']}", headers=HEADERS, timeout=TIMEOUT)
            detail.raise_for_status()
            desc = _html_to_text(detail.json().get("content"))
        except (requests.RequestException, ValueError, KeyError):
            pass
        company = j.get("company_name") or board.title()
        jobs.append(_job(
            j.get("title", ""), company, (j.get("location") or {}).get("name", ""),
            j.get("absolute_url", ""), desc, posted, "greenhouse", board,
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
# https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite -> "nvidia|wd5|NVIDIAExternalCareerSite",
# optionally followed by "|<Company name>" when the tenant is cryptic ("sec|wd3|Samsung_Careers|Samsung").
# The posting's own hiringOrganization is a legal entity ("0090 CORP-Corporate Office"), so it isn't used.
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
    tenant, wd, site, *name = spec.split("|")
    company = name[0] if name else tenant.title()
    spec = f"{tenant}|{wd}|{site}"
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
            if posted is None or posted < cutoff or not _MAYBE_INTERN.search(j.get("title", "")):
                continue
            desc = ""
            try:
                info = requests.get(base + j["externalPath"], headers=headers, timeout=TIMEOUT).json()
                desc = _html_to_text((info.get("jobPostingInfo") or {}).get("jobDescription"))
            except (requests.RequestException, ValueError, KeyError):
                pass
            jobs.append(_job(
                j.get("title", ""), company, j.get("locationsText", ""),
                f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{j.get('externalPath', '')}", desc,
                posted, "workday", spec,
            ))
        if len(postings) < 20:
            break
    return jobs


def _eightfold_get(url, params):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_eightfold(spec, pages=5, max_age_hours=72):
    """Eightfold career sites (Netflix, Qualcomm). spec: "Company|careers host|domain", e.g.
    "Netflix|explore.jobs.netflix.net|netflix.com". Eightfold has two public APIs and each site
    enables only one: the newer "pcsx" search (Qualcomm) or the older apply/v2 (Netflix).
    Descriptions are fetched only for fresh internship-looking postings."""
    company, host, domain = spec.split("|")
    root = f"https://{host}/api"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    try:
        _eightfold_get(f"{root}/pcsx/search", {"domain": domain, "query": "intern", "start": 0})
        pcsx = True
    except requests.HTTPError:
        pcsx = False
    jobs, start = [], 0
    for page in range(pages):
        try:
            if pcsx:
                data = _eightfold_get(f"{root}/pcsx/search", {"domain": domain, "query": "intern",
                                                              "start": start, "sort_by": "timestamp"})["data"]
            else:
                data = _eightfold_get(f"{root}/apply/v2/jobs", {"domain": domain, "query": "intern",
                                                                "start": start, "num": 50, "sort_by": "new"})
        except requests.HTTPError:
            if page == 0:
                raise
            break   # Qualcomm rate-limits quick paging (429); keep the newest pages already read
        positions = data.get("positions") or []
        stale = False
        for p in positions:
            posted = _parse_dt(p.get("postedTs") if pcsx else p.get("t_create"))
            stale = stale or (posted is not None and posted < cutoff)
            if posted is None or posted < cutoff or not _MAYBE_INTERN.search(p.get("name", "")):
                continue
            desc = ""
            try:
                if pcsx:
                    info = _eightfold_get(f"{root}/pcsx/position_details",
                                          {"position_id": p["id"], "domain": domain, "hl": "en"}).get("data") or {}
                    desc = _html_to_text(info.get("jobDescription"))
                else:
                    desc = _html_to_text(_eightfold_get(f"{root}/apply/v2/jobs/{p['id']}",
                                                        {"domain": domain}).get("job_description"))
            except (requests.RequestException, ValueError, KeyError):
                pass
            url = p.get("canonicalPositionUrl") or f"https://{host}{p.get('positionUrl', '')}"
            location = "; ".join(p.get("locations") or []) or p.get("location", "")
            jobs.append(_job(p.get("name", ""), company, location, url, desc, posted, "eightfold", spec))
        start += len(positions)
        if not positions or stale or start >= (data.get("count") or 0):
            break   # newest first, so a page reaching past the age window is the last one needed
    return jobs


def fetch_successfactors(spec):
    """SAP SuccessFactors career sites (e.g. Sephora's jobs.sephora.com) publish an RSS feed of
    their newest postings, with full descriptions. spec: "Company|https://jobs.example.com".
    The feed holds the 20 newest postings site-wide; checked every 30 minutes that's plenty."""
    import xml.etree.ElementTree as ET
    company, base = spec.split("|", 1)
    resp = requests.get(f"{base.rstrip('/')}/services/rss/job/", params={"locale": "en_US"},
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
    resp.raise_for_status()
    jobs = []
    for item in ET.fromstring(resp.content).findall(".//item"):
        raw = (item.findtext("title") or "").strip()
        m = re.match(r"^(.*)\s+\(([^()]*)\)$", raw)   # "Title (City, ST, US, ...)"
        title, location = (m.group(1), m.group(2)) if m else (raw, "")
        jobs.append(_job(title, company, location, (item.findtext("link") or "").strip(),
                         _html_to_text(item.findtext("description")), _parse_dt(item.findtext("pubDate")),
                         "successfactors", base))
    return jobs


def fetch_oracle(spec, max_age_hours=72):
    """Oracle Recruiting Cloud career sites (JPMorgan, TI, Emerson, Dell, Honeywell...): a public REST
    API behind every .../hcmUI/CandidateExperience/en/sites/<site>/job/<id> page.
    spec: "Company|<host>|<site number>", e.g. "Emerson|hdjq.fa.us2.oraclecloud.com|CX_1"."""
    company, host, site = spec.split("|")
    api = f"https://{host}/hcmRestApi/resources/latest"
    resp = requests.get(f"{api}/recruitingCEJobRequisitions", headers=HEADERS, timeout=TIMEOUT, params={
        "onlyData": "true", "expand": "requisitionList",
        "finder": f"findReqs;siteNumber={site},keyword=intern,limit=50,sortBy=POSTING_DATES_DESC"})
    resp.raise_for_status()
    items = resp.json().get("items") or [{}]
    jobs, cutoff = [], datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for r in items[0].get("requisitionList") or []:
        posted = _parse_dt(r.get("PostedDate"))
        if posted is None or posted < cutoff or not _MAYBE_INTERN.search(r.get("Title", "")):
            continue
        desc = ""
        try:
            d = requests.get(f"{api}/recruitingCEJobRequisitionDetails", headers=HEADERS, timeout=TIMEOUT, params={
                "onlyData": "true", "expand": "all", "finder": f'ById;Id="{r["Id"]}",siteNumber={site}'}).json()
            info = (d.get("items") or [{}])[0]
            desc = "\n\n".join(_html_to_text(info.get(k)) for k in
                                ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
                                if info.get(k))
        except (requests.RequestException, ValueError, KeyError):
            pass
        jobs.append(_job(r.get("Title", ""), company, r.get("PrimaryLocation", ""),
                         f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
                         desc, posted, "oracle", spec))
    return jobs


def fetch_workable(account):
    """Workable's public job widget, with descriptions. account: apply.workable.com/<account>/."""
    resp = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{account}",
                        params={"details": "true"}, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs") or []:
        location = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        if str(j.get("telecommuting")).lower() == "true":
            location = f"{location} (Remote)".strip()
        jobs.append(_job(j.get("title", ""), data.get("name") or account, location, j.get("url", ""),
                         _html_to_text(j.get("description")),
                         _parse_dt(j.get("published_on") or j.get("created_at")), "workable", account))
    return jobs


def fetch_rippling(board, max_age_hours=72):
    """Rippling ATS boards (ats.rippling.com/<board>/jobs/...). The list has no dates, so each
    internship-looking posting is read for its date and description."""
    base = f"https://ats.rippling.com/api/v2/board/{board}/jobs"
    jobs, cutoff = [], datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for page in range(5):
        resp = requests.get(base, params={"page": page, "pageSize": 100}, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for j in data.get("items") or []:
            if not _MAYBE_INTERN.search(j.get("name", "")):
                continue
            try:
                info = requests.get(f"{base}/{j['id']}", headers=HEADERS, timeout=TIMEOUT).json()
            except (requests.RequestException, ValueError, KeyError):
                continue
            posted = _parse_dt(info.get("createdOn"))
            if posted is None or posted < cutoff:
                continue
            desc = info.get("description")
            if isinstance(desc, dict):   # {"company": html, "role": html}
                desc = "\n\n".join(_html_to_text(v) for v in desc.values() if isinstance(v, str))
            else:
                desc = _html_to_text(desc)
            location = "; ".join(l.get("name", "") for l in j.get("locations") or [])
            jobs.append(_job(j.get("name", ""), info.get("companyName") or board.title(), location,
                             j.get("url", ""), desc, posted, "rippling", board))
        if page + 1 >= (data.get("totalPages") or 1):
            break
    return jobs


def board_from_listing(url, company):
    """(ats, spec) for any posting link a fetcher here can read, else None. Like _ats_from_url, plus
    the sources that need a company name or aren't used for application packets."""
    site, board = _ats_from_url(url)
    if site != "other":
        return site, (f"{board}|{company}" if site == "workday" and company else board)
    m = re.search(r"https://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)", url)
    if m:   # the same Workday site under another host
        wd, tenant, site_name = m.groups()
        return "workday", f"{tenant}|{wd}|{site_name}" + (f"|{company}" if company else "")
    m = re.search(r"https://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}/sites/([\w-]+)/", url)
    if m and company:
        return "oracle", f"{company}|{m.group(1)}|{m.group(2)}"
    m = re.search(r"apply\.workable\.com/(?!j/)([\w-]+)/", url)
    if m:
        return "workable", m.group(1)
    m = re.search(r"ats\.rippling\.com/([\w-]+)/jobs/", url)
    if m:
        return "rippling", m.group(1)
    return None


def _board_key(ats, spec):
    """Identity of a board regardless of the optional company name: ("workday", "sec|wd3|samsung_careers")."""
    parts = spec.lower().split("|")
    if ats == "workday":
        parts = parts[:3]
    elif ats == "oracle":
        parts = parts[1:]
    return ats, "|".join(parts)


def harvest_boards(listings):
    """Every company board behind the Simplify list, open listings or not: {ats: [spec, ...]}.
    A company that posted one internship there this season is worth watching for the next."""
    out, seen = {}, set()
    for j in listings:
        found = board_from_listing(j.get("url", ""), (j.get("company_name") or "").replace("|", " ").strip())
        if not found or _board_key(*found) in seen:
            continue
        seen.add(_board_key(*found))
        out.setdefault(found[0], []).append(found[1])
    return out


def rotation_slice(cfg_lists, harvested, groups, now=None):
    """The harvested boards for this run: those not configured already, split into `groups` stable
    groups by a hash of the board, one group per 30-minute run, so each is checked every
    groups x 30 minutes while the configured boards are checked every run."""
    import zlib
    configured = {_board_key(ats, spec) for ats, specs in cfg_lists.items() if isinstance(specs, list)
                  for spec in specs}
    turn = int((now if now is not None else datetime.now(timezone.utc).timestamp()) // 1800) % max(groups, 1)
    out = {}
    for ats, specs in harvested.items():
        for spec in specs:
            key = _board_key(ats, spec)
            if key not in configured and zlib.crc32("|".join(key).encode()) % max(groups, 1) == turn:
                out.setdefault(ats, []).append(spec)
    return out


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
    return simplify_jobs(download_simplify(url))


def download_simplify(url=SIMPLIFY_URL):
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.json()


def simplify_jobs(listings):
    jobs = []
    for j in listings:
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


def is_internship_title(title, intern_keywords, exclude_titles):
    t = f" {str(title).lower()} "
    if any(x in t for x in exclude_titles):
        return False
    # Whole-word match so "intern" doesn't hit "Internal", "International", "Internals"
    return any(re.search(rf"\b{re.escape(k)}s?\b", t) for k in intern_keywords)


def title_matches(title, role_keywords, intern_keywords, exclude_titles):
    t = f" {str(title).lower()} "
    return is_internship_title(title, intern_keywords, exclude_titles) and any(k in t for k in role_keywords)


def is_watched_internship(job, watch, intern_keywords):
    """Any internship at a company on the watch list, whatever the role (e.g. every Sephora
    internship), so it's alerted even when it isn't a tech role."""
    company = str(job.get("company", "")).lower()
    if not any(w.lower() in company for w in watch):
        return False
    t = f" {str(job.get('title', '')).lower()} "
    return any(re.search(rf"\b{re.escape(k)}s?\b", t) for k in intern_keywords)


def _unique_workday(specs):
    """Drop a Workday spec whose tenant/site is already listed (a learned board has no company
    name, so it can repeat a configured one). The first, i.e. configured, spelling wins."""
    seen, out = set(), []
    for spec in specs:
        key = "|".join(spec.split("|")[:3]).lower()
        if key not in seen:
            seen.add(key)
            out.append(spec)
    return out


def fetch_all(cfg, role_keywords, intern_keywords, exclude_titles, watch=(), also=None):
    """Return recent, title-matching postings from every configured career site. Internships at
    watched companies are kept whatever the role, and marked job["watched"] = True.
    also(title) -> truthy keeps other internships too (the shared channel's tracks), marked
    job["shared_only"] = True so they're only checked for that channel."""
    max_age = timedelta(hours=cfg.get("max_age_hours", 72))
    now = datetime.now(timezone.utc)

    aged = {"max_age_hours": cfg.get("max_age_hours", 72)}   # these filter by age before fetching descriptions
    simplify = []
    if cfg.get("simplify", True):
        try:
            listings = download_simplify()
            simplify = simplify_jobs(listings)
            if cfg.get("harvest_simplify_boards"):
                extra = rotation_slice(cfg, harvest_boards(listings), cfg.get("harvest_groups", 4))
                cfg = {**cfg, **{ats: list(cfg.get(ats, [])) + specs for ats, specs in extra.items()}}
                logging.info(f"Harvested boards this run: { {k: len(v) for k, v in extra.items()} }")
        except Exception as e:
            logging.warning(f"Simplify list failed: {e}")
    sources = (
        [(fetch_greenhouse, b, aged) for b in cfg.get("greenhouse", [])]
        + [(fetch_lever, c, {}) for c in cfg.get("lever", [])]
        + [(fetch_ashby, b, {}) for b in cfg.get("ashby", [])]
        + [(fetch_smartrecruiters, c, {}) for c in cfg.get("smartrecruiters", [])]
        + [(fetch_workday, w, aged) for w in _unique_workday(cfg.get("workday", []))]
        + [(fetch_successfactors, s, {}) for s in cfg.get("successfactors", [])]
        + [(fetch_eightfold, e, aged) for e in cfg.get("eightfold", [])]
        + [(fetch_oracle, o, aged) for o in cfg.get("oracle", [])]
        + [(fetch_workable, w, {}) for w in cfg.get("workable", [])]
        + [(fetch_rippling, r, aged) for r in cfg.get("rippling", [])]
        + [(fetch_amazon, q, {}) for q in cfg.get("amazon_queries", [])]
        + [(lambda _: simplify, "simplify", {})]
    )

    def run(source):
        fetch, arg, kwargs = source
        try:
            return fetch(arg, **kwargs)
        except Exception as e:
            logging.warning(f"Career site {getattr(fetch, '__name__', 'source')}({arg}) failed: {e}")
            return None

    # hundreds of boards: fetched in parallel (each is a different host or board), results kept in config order
    with ThreadPoolExecutor(max_workers=12) as pool:
        fetched = list(pool.map(run, sources))

    results, seen_urls = [], set()
    for (fetch, arg, _), jobs in zip(sources, fetched):
        if jobs is None:
            continue
        kept = 0
        for job in jobs:
            if not job["job_url"] or job["job_url"] in seen_urls:
                continue
            watched = is_watched_internship(job, watch, intern_keywords)
            shared_only = False
            if not watched and not title_matches(job["title"], role_keywords, intern_keywords, exclude_titles):
                shared_only = bool(also and is_internship_title(job["title"], intern_keywords, exclude_titles)
                                   and also(job["title"]))
                if not shared_only:
                    continue
            job["watched"] = watched
            job["shared_only"] = shared_only
            posted = job["date_posted"]
            if posted is None or now - posted > max_age:
                continue
            seen_urls.add(job["job_url"])
            results.append(job)
            kept += 1
        logging.info(f"Career site {getattr(fetch, '__name__', 'source')}({arg}): {len(jobs)} postings, {kept} new-ish matches")
    return results
