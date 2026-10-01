#!/usr/bin/env python3
"""
Application packets: everything needed to submit an application, prepared ahead of time.

The scraper enqueues every career-site job it alerts on (Greenhouse, Lever, Ashby). Each
run then preps a few queued jobs: tailored resume + cover letter (tailor.py), the form's
real question list (read from the ATS), and a drafted answer for every question. The
packet lands in output/packets/<slug>/ and a summary goes to Discord.

Submitting is done by hand (with Claude filling the form in Chrome): every ATS here puts
a CAPTCHA on its application form, so nothing is submitted from the server.

  python apply_prep.py --pending 3        # prep up to 3 queued jobs
  python apply_prep.py --url <job_url>    # enqueue + prep one job now
  python apply_prep.py --list             # show the queue
  python apply_prep.py --submitted <url>  # mark submitted (DB + Google Sheet)
  python apply_prep.py --skip <url>       # drop a job from the queue
"""

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(SCRIPT_DIR, "jobs.db")
OUTPUT_DIR   = os.getenv("OUTPUT_DIR", os.path.join(SCRIPT_DIR, "output"))
PACKETS_DIR  = os.path.join(OUTPUT_DIR, "packets")
ANSWERS_PATH = os.path.join(SCRIPT_DIR, "application_answers.yaml")
KB_PATH      = os.path.join(SCRIPT_DIR, "experience_kb.yaml")
WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL")
BOT_TOKEN    = os.getenv("DISCORD_BOT_TOKEN")
APPLY_CHANNEL_ID = os.getenv("APPLICATIONS_CHANNEL_ID")  # #applications: packet-ready + applied notices
DISCORD_USER_ID  = os.getenv("DISCORD_USER_ID")
DEFAULT_RESUME   = "General/default resume"
from claude_json import DRAFT_MODEL as MODEL
HEADERS      = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT      = 20
MAX_OPTIONS  = 60   # longer option lists (e.g. every university) aren't sent to Claude

SUPPORTED_ATS = ("greenhouse", "lever", "ashby")


# ── Queue (jobs.db) ───────────────────────────────────────────────────────────

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS apply_queue (
            job_url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            ats TEXT,
            board TEXT,
            fit_score INTEGER,
            status TEXT DEFAULT 'queued',
            packet_dir TEXT,
            note TEXT,
            date_added TEXT,
            date_updated TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS learned_boards (
            ats TEXT,
            board TEXT,
            source TEXT,
            date_added TEXT,
            PRIMARY KEY (ats, board)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tailored_resumes (
            job_url TEXT PRIMARY KEY,
            filename TEXT,
            message_url TEXT,
            date_added TEXT
        )
    """)
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def enqueue(conn, job, fit_score=None):
    """Add an alerted career-site job to the queue. Returns True if newly added."""
    if job.get("site") not in SUPPORTED_ATS or not job.get("job_url"):
        return False
    cur = conn.execute(
        """INSERT OR IGNORE INTO apply_queue
           (job_url, title, company, ats, board, fit_score, status, date_added, date_updated)
           VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (job["job_url"], job.get("title", ""), job.get("company", ""), job["site"],
         job.get("board", ""), fit_score, _now(), _now()),
    )
    conn.commit()
    return cur.rowcount == 1


def set_status(conn, job_url, status, packet_dir=None, note=None):
    conn.execute(
        """UPDATE apply_queue SET status = ?, packet_dir = COALESCE(?, packet_dir),
           note = COALESCE(?, note), date_updated = ? WHERE job_url = ?""",
        (status, packet_dir, note, _now(), job_url),
    )
    conn.commit()


LEARNABLE_ATS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workday")


def learn_board(conn, job, source):
    """Remember the job board of an alerted job, so the scraper checks that company directly
    from now on (see learned_boards). Returns True if it's new."""
    ats, board = job.get("site"), job.get("board")
    if ats not in LEARNABLE_ATS or not board:
        return False
    cur = conn.execute("INSERT OR IGNORE INTO learned_boards VALUES (?, ?, ?, ?)", (ats, board, source, _now()))
    conn.commit()
    return cur.rowcount == 1


def with_learned_boards(career_sites_cfg, conn):
    """The configured career-site lists plus every learned board (config file untouched)."""
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in career_sites_cfg.items()}
    for ats, board in conn.execute("SELECT ats, board FROM learned_boards"):
        boards = cfg.setdefault(ats, [])
        if board not in boards:
            boards.append(board)
    return cfg


def record_tailored(job_url, filename, message_url=None):
    """Remember which resume file went with a job (and the Discord message holding it)."""
    if not job_url or not filename:
        return
    conn = db_conn()
    conn.execute("INSERT OR REPLACE INTO tailored_resumes VALUES (?, ?, ?, ?)",
                 (job_url, filename, message_url, _now()))
    conn.commit()
    conn.close()


def resume_used(job_url):
    """Resume Used cell for the tracker: link to the tailored resume, or the default resume."""
    import sheets_sync
    conn = db_conn()
    row = conn.execute("SELECT filename, message_url FROM tailored_resumes WHERE job_url = ?",
                       (job_url,)).fetchone()
    conn.close()
    return sheets_sync.resume_cell(*row) if row else DEFAULT_RESUME


# ── Resolving the application form ────────────────────────────────────────────

def detect_ats(url):
    host = urlparse(url).netloc.lower()
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "greenhouse.io" in host or "gh_jid=" in url:
        return "greenhouse"
    return None


def greenhouse_ids(url, board=""):
    """(board, job_id) from a Greenhouse URL. Company-hosted URLs only carry gh_jid, so
    the board must come from the scraper (career_sites sets it)."""
    qs = parse_qs(urlparse(url).query)
    job_id = (qs.get("gh_jid") or [""])[0]
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url)
    if m:
        board = board or m.group(1)
        job_id = job_id or m.group(2)
    if not job_id:
        m = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
        job_id = m.group(1) if m else ""
    return board, job_id


def lever_ids(url):
    m = re.search(r"jobs\.lever\.co/([\w-]+)/([0-9a-f-]{36})", url)
    return (m.group(1), m.group(2)) if m else ("", "")


def ashby_ids(url):
    m = re.search(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})", url)
    return (m.group(1), m.group(2)) if m else ("", "")


def _q(key, label, qtype, required=False, options=None):
    return {"key": key, "label": label, "type": qtype, "required": bool(required),
            "options": options or []}


GH_TYPES = {
    "input_text": "text", "textarea": "textarea", "input_file": "file",
    "multi_value_single_select": "select", "multi_value_multi_select": "multiselect",
}


def greenhouse_form(board, job_id):
    resp = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}",
        params={"questions": "true"}, headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    questions = []
    for q in data.get("questions", []):
        for f in q.get("fields", []):
            qtype = GH_TYPES.get(f.get("type"))
            if not qtype or f["name"] in ("resume_text", "cover_letter_text"):
                continue
            questions.append(_q(f["name"], q.get("label", ""), qtype, q.get("required"),
                                [v["label"] for v in f.get("values", [])]))
            break  # one input per question (the others are alternates, e.g. paste vs upload)
    # Newer "Candidate Demographic Survey" section (separate from compliance); field ids are numeric
    for q in (data.get("demographic_questions") or {}).get("questions", []):
        qtype = GH_TYPES.get(q.get("type"))
        if qtype in ("select", "multiselect"):
            questions.append(_q(str(q["id"]), q.get("label", ""), qtype, q.get("required"),
                                [a["label"] for a in q.get("answer_options", [])]))
    for block in data.get("compliance") or []:
        for q in block.get("questions", []):
            for f in q.get("fields", []):
                questions.append(_q(f["name"], q.get("label", ""), "select", q.get("required"),
                                    [v["label"] for v in f.get("values", [])]))
    # Present on every Greenhouse job board form but not in the API
    questions += [
        _q("country", "Country", "select", True, ["United States"]),
        _q("candidate-location", "Location (City)", "location", True),
    ]
    return {
        "title": data.get("title", ""), "company": data.get("company_name") or board.title(),
        "form_url": f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}",
        "description": BeautifulSoup(requests.utils.unquote(data.get("content", "")), "html.parser")
                       .get_text("\n", strip=True) if data.get("content") else "",
        "questions": questions,
    }


def lever_form(company, posting_id):
    form_url = f"https://jobs.lever.co/{company}/{posting_id}/apply"
    soup = BeautifulSoup(requests.get(form_url, headers=HEADERS, timeout=TIMEOUT).text, "html.parser")
    questions, seen = [], set()
    for li in soup.select("li.application-question"):
        label_el = li.select_one(".application-label")
        label = label_el.get_text(" ", strip=True) if label_el else ""
        required = "✱" in label
        label = label.replace("✱", "").strip()
        inputs = [i for i in li.select("input, textarea, select") if i.get("type") != "hidden"]
        if not inputs:
            continue
        first = inputs[0]
        name = first.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        itype = first.get("type") or first.name
        if first.name == "select":
            opts = [o.get_text(strip=True) for o in first.select("option")]
            if opts and re.match(r"(select|please select|click here)", opts[0], re.I):
                opts = opts[1:]
            questions.append(_q(name, label, "select", required, opts))
        elif itype == "radio":
            questions.append(_q(name, label, "select", required, [i.get("value", "") for i in inputs]))
        elif itype == "checkbox":
            questions.append(_q(name, label, "multiselect", required, [i.get("value", "") for i in inputs]))
        elif itype == "file":
            questions.append(_q(name, label, "file", required))
        elif first.name == "textarea":
            questions.append(_q(name, label, "textarea", required))
        else:
            questions.append(_q(name, label, "text", required))
    title = soup.select_one(".posting-header h2")
    return {"title": title.get_text(strip=True) if title else "", "company": company.title(),
            "form_url": form_url, "questions": questions}


ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id title descriptionPlainText
    applicationForm { sections { title fieldEntries { ... on FormFieldEntry { id field isRequired } } } }
  }
}"""

ASHBY_TYPES = {
    "String": "text", "Email": "text", "Phone": "text", "Number": "text", "Date": "text",
    "LongText": "textarea", "File": "file", "Location": "location",
    "ValueSelect": "select", "MultiValueSelect": "multiselect", "Boolean": "boolean",
}


def ashby_form(org, posting_id):
    resp = requests.post(
        "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
        json={"operationName": "ApiJobPosting", "query": ASHBY_QUERY,
              "variables": {"organizationHostedJobsPageName": org, "jobPostingId": posting_id}},
        headers=HEADERS, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    posting = (resp.json().get("data") or {}).get("jobPosting") or {}
    questions = []
    for section in (posting.get("applicationForm") or {}).get("sections", []):
        for entry in section.get("fieldEntries", []):
            f = entry.get("field") or {}
            qtype = ASHBY_TYPES.get(f.get("type"), "text")
            opts = [v.get("label", "") for v in f.get("selectableValues") or []]
            if qtype == "boolean":
                qtype, opts = "select", ["Yes", "No"]
            questions.append(_q(f.get("path", ""), f.get("title", ""), qtype, entry.get("isRequired"), opts))
    return {
        "title": posting.get("title", ""), "company": org.title(),
        "form_url": f"https://jobs.ashbyhq.com/{org}/{posting_id}/application",
        "description": posting.get("descriptionPlainText", ""),
        "questions": questions,
    }


def resolve_form(job_url, ats=None, board=""):
    ats = ats or detect_ats(job_url)
    if ats == "greenhouse":
        board, job_id = greenhouse_ids(job_url, board)
        if not board or not job_id:
            raise ValueError(f"Can't find Greenhouse board/job id in {job_url}")
        form = greenhouse_form(board, job_id)
    elif ats == "lever":
        form = lever_form(*lever_ids(job_url))
    elif ats == "ashby":
        form = ashby_form(*ashby_ids(job_url))
    else:
        raise ValueError(f"Unsupported application site: {job_url}")
    form["ats"] = ats
    return form


# ── Finding LinkedIn jobs on the company's own job board ─────────────────────
# LinkedIn hides the real application link behind a sign-in, but most companies post the
# same role on their own ATS board, which is public. Guess the board name from the company
# name, then accept a posting only if the title and the description both match.

SLUG_SUFFIXES = ("", "labs", "hq", "inc", "ai", "app", "careers", "jobs")
_COMPANY_NOISE = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology)\b\.?", re.I)
TITLE_SIMILARITY = 0.85   # SequenceMatcher ratio on normalized titles
DESC_OVERLAP = 0.25       # Jaccard overlap of description word sets


def slug_candidates(company):
    """Board names to try for a company, e.g. "Clay" -> clay, claylabs, clayhq, ...;
    "Scale AI" -> scaleai, scale-ai, scale, ..."""
    words = re.findall(r"[a-z0-9]+", _COMPANY_NOISE.sub(" ", str(company).lower()))
    if not words:
        return []
    bases = ["".join(words), "-".join(words)]
    if len(words) > 1:
        bases.append(words[0])
    out = []
    for base in bases:
        for suffix in SLUG_SUFFIXES:
            slug = base + suffix
            if slug not in out:
                out.append(slug)
    return out


_SEASON_WORDS = {"summer", "fall", "autumn", "winter", "spring", "start", "internship", "program"}


def _norm_title(title):
    """Title reduced to the role: drops "(London)", "[2027]", seasons and years, so
    "Software Engineer Intern - Summer 2027" and "Software Engineer Intern (Summer 2027)" match."""
    t = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", str(title).lower())
    words = [w for w in re.findall(r"[a-z0-9]+", t)
             if w not in _SEASON_WORDS and not re.fullmatch(r"(19|20)\d\d", w)]
    return " ".join(words)


def _words(text):
    return set(re.findall(r"[a-z]{4,}", str(text).lower()))


def _desc_overlap(desc_a, desc_b):
    wa, wb = _words(desc_a), _words(desc_b)
    if len(wa) < 30 or len(wb) < 30:
        return None   # too short to compare
    return len(wa & wb) / len(wa | wb)


def same_job(title_a, desc_a, title_b, desc_b):
    ta, tb = _norm_title(title_a), _norm_title(title_b)
    if not ta or not tb or SequenceMatcher(None, ta, tb).ratio() < TITLE_SIMILARITY:
        return False
    overlap = _desc_overlap(desc_a, desc_b)
    if overlap is None:
        return ta == tb   # can't compare descriptions: only an exact title counts
    return overlap >= DESC_OVERLAP


def _location_words(text):
    return set(re.findall(r"[a-z]{3,}", str(text).lower())) - {"remote", "hybrid", "office", "united", "states"}


def pick_match(matches, description="", location="", title=""):
    """One posting from several that match the title (e.g. the same internship in SF and
    London, or Summer and Winter): prefer the matching location, then the full title
    including what's in parentheses, then the closest description.
    Returns None when it's still a toss-up."""
    if len(matches) <= 1:
        return matches[0] if matches else None
    loc = _location_words(location)
    if loc:
        by_loc = [m for m in matches if loc & _location_words(m.get("location"))]
        if len(by_loc) == 1:
            return by_loc[0]
        matches = by_loc or matches
    full = set(re.findall(r"[a-z0-9]+", str(title).lower()))
    if full:
        by_title = sorted(((len(full & set(re.findall(r"[a-z0-9]+", str(m.get("title")).lower()))), i)
                           for i, m in enumerate(matches)), reverse=True)
        best = [matches[i] for n, i in by_title if n == by_title[0][0]]
        if len(best) == 1:
            return best[0]
        matches = best
    scored = sorted(((_desc_overlap(description, m.get("description")) or 0, i) for i, m in enumerate(matches)),
                    reverse=True)
    if scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return matches[scored[0][1]]
    return None


def find_on_ats(company, title, description="", location="", fetchers=None):
    """Look for this job on the company's Greenhouse / Lever / Ashby board.
    Returns a career_sites-style job dict (with site and board) or None."""
    import career_sites
    fetchers = fetchers or {"greenhouse": career_sites.fetch_greenhouse,
                            "lever": career_sites.fetch_lever,
                            "ashby": career_sites.fetch_ashby}
    for slug in slug_candidates(company):
        for site, fetch in fetchers.items():
            try:
                postings = fetch(slug)
            except Exception:
                continue   # no board by that name (404) or a network error
            matches = [j for j in postings
                       if same_job(title, description, j.get("title"), j.get("description"))]
            if matches:
                job = pick_match(matches, description, location, title)
                if job:
                    logging.info(f"Found {company} / {title} on {site} board '{slug}': {job.get('job_url')}")
                else:
                    logging.info(f"{company} / {title}: {len(matches)} similar postings on {site} "
                             f"board '{slug}', none clearly the same; not queued")
                return job
    return None


# ── Drafting answers ──────────────────────────────────────────────────────────

def _questions_for_prompt(questions):
    out = []
    for q in questions:
        if q["type"] == "file":
            continue
        item = {"key": q["key"], "label": q["label"], "type": q["type"], "required": q["required"]}
        if q["options"]:
            if len(q["options"]) <= MAX_OPTIONS:
                item["options"] = q["options"]
            else:
                item["note"] = f"{len(q['options'])} options (searchable list); give the value to search for"
        out.append(item)
    return out


def build_answer_prompt(job, form, kb, standard):
    return f"""You are filling out a job application form for the candidate below.
Answer every question using ONLY the candidate's real information. Never invent facts,
numbers, employers, or skills.

STANDARD ANSWERS (use these for matching questions):
{yaml.dump(standard, default_flow_style=False, allow_unicode=True)}
(The candidate knowledge base is in the system prompt.)

JOB: {job.get("title")} at {job.get("company")}
JOB DESCRIPTION (excerpt):
{(form.get("description") or job.get("description") or "")[:3500]}

QUESTIONS (JSON):
{json.dumps(_questions_for_prompt(form["questions"]), indent=1)}

RULES:
- Where the standard answers and the knowledge base disagree (e.g. email), the standard
  answers win.
- Use legal_name_on_government_id ONLY for a question asking for a full legal name or the
  name on a government ID. Every other name field is first name "Haatim", last name "Ali".
- "select" answers must be exactly one of the listed options, copied verbatim.
  "multiselect" answers are a list of listed options.
- If a select has no listed options (searchable list), answer with the text to search for.
- Voluntary self-identification (gender, race, ethnicity, veteran, disability, pronouns,
  sexual orientation): use self_identification from the standard answers, picking the
  closest listed option. Anything it doesn't cover (or says to decline): pick the
  decline / prefer-not-to-say option; if there is none, "".
- Free-text questions (why this company, projects, etc.): follow essay_style from the
  standard answers. Reference something specific from the job description.
  Never claim personal experiences, opinions or habits that are not in the knowledge base
  (e.g. "I use your product every day", "I've followed your company for years").
  Motivation must come from the job description plus the candidate's real work.
- No em dashes, en dashes, or double hyphens ("--") anywhere. Use commas or periods.
- Optional questions you have no real answer for (e.g. preferred name): "".
- If a question asks for a fact that is NOT in the standard answers or the knowledge base
  (e.g. GPA, age, a specific certification), answer "" and list its key in "unknown".
  Do not guess.

Put every question's answer under its key in "answers" ("" or [] where you have no real
answer: never invent one), and list the keys of questions you couldn't answer from the facts in "unknown"."""


def answer_schema(questions):
    """Structured-output schema with one property per question, so every answer is under a
    real question key and every dropdown answer is one of that question's actual options
    (long searchable lists like every university are free text instead).
    "No answer" is "" (or [] for multiselects) rather than null: the API rejects schemas with
    many nullable/union fields, and validate_answers treats both the same."""
    props = {}
    for q in questions:
        if q["type"] == "file":
            continue
        opts = q["options"] if 0 < len(q["options"]) <= MAX_OPTIONS else []
        if q["type"] == "select" and opts:
            props[q["key"]] = {"type": "string", "enum": opts + ([""] if "" not in opts else [])}
        elif q["type"] == "multiselect" and opts:
            props[q["key"]] = {"type": "array", "items": {"type": "string", "enum": opts}}
        elif q["type"] == "multiselect":
            props[q["key"]] = {"type": "array", "items": {"type": "string"}}
        else:
            props[q["key"]] = {"type": "string"}
    return {"type": "object", "additionalProperties": False, "required": ["answers", "unknown"],
            "properties": {
                "answers": {"type": "object", "properties": props, "required": list(props),
                            "additionalProperties": False},
                "unknown": {"type": "array", "items": {"type": "string", "enum": list(props) or [""]}},
            }}


def draft_answers(job, form, client=None):
    import anthropic
    from claude_json import ask_json, cached_system, kb_system_text
    client = client or anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    kb = yaml.safe_load(open(KB_PATH))
    standard = yaml.safe_load(open(ANSWERS_PATH))
    result = ask_json(client, build_answer_prompt(job, form, kb, standard), answer_schema(form["questions"]),
                      model=MODEL, max_tokens=16000, system=cached_system(kb_system_text(kb)))
    result["answers"] = {k: strip_dashes(v) for k, v in (result.get("answers") or {}).items()}
    return validate_answers(form["questions"], result.get("answers") or {}, result.get("unknown") or [])


def strip_dashes(value):
    """Essays: turn em/en dashes and '--' used as dashes into commas (option labels untouched)."""
    if not isinstance(value, str) or len(value) < 40:
        return value
    return re.sub(r"\s*(?:—|–|--)\s*", ", ", value)


def parse_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(m.group(0) if m else text)
    except (json.JSONDecodeError, AttributeError):
        return {}


def validate_answers(questions, answers, unknown):
    """Keep only answers that fit their question; anything required and missing is flagged."""
    by_key = {q["key"]: q for q in questions}
    clean, flagged = {}, set(k for k in unknown if k in by_key)
    for key, value in answers.items():
        q = by_key.get(key)
        if q is None or value in (None, "", []):
            continue
        opts = q["options"] if len(q["options"]) <= MAX_OPTIONS else []
        if q["type"] == "select" and opts and value not in opts:
            flagged.add(key)
            continue
        if q["type"] == "multiselect":
            value = [v for v in (value if isinstance(value, list) else [value]) if not opts or v in opts]
            if not value:
                flagged.add(key)
                continue
        clean[key] = value
    for q in questions:
        if q["required"] and q["type"] != "file" and q["key"] not in clean:
            flagged.add(q["key"])
    return clean, sorted(flagged)


# ── Packets ───────────────────────────────────────────────────────────────────

def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def write_packet(job, form, answers, unknown, resume_pdf, cover_pdf):
    slug = slugify(f"{job.get('company', '')}-{job.get('title', '')}")
    pdir = os.path.join(PACKETS_DIR, slug)
    os.makedirs(pdir, exist_ok=True)
    import tailor
    files = {}
    for kind, src in (("resume", resume_pdf), ("cover_letter", cover_pdf)):
        if src and os.path.exists(src):
            dst = os.path.join(pdir, tailor.output_filename(job.get("company"), kind))
            shutil.copy(src, dst)
            files[kind] = os.path.basename(dst)
    packet = {
        "job_url": job["job_url"], "form_url": form["form_url"], "ats": form["ats"],
        "company": job.get("company"), "title": job.get("title"),
        "prepared": _now(), "files": files,
        "questions": [
            {**q, "answer": answers.get(q["key"]), "needs_you": q["key"] in unknown,
             "options": q["options"] if len(q["options"]) <= MAX_OPTIONS else []}
            for q in form["questions"]
        ],
    }
    with open(os.path.join(pdir, "packet.json"), "w") as f:
        json.dump(packet, f, indent=2, ensure_ascii=False)
    return pdir, packet


def post_discord(text, ping=False, attachment=None):
    """Post to the #applications channel as the bot (falls back to the alerts webhook).

    attachment: (path, filename) to upload with the message. Returns a link to the posted
    message when it went to #applications, else None."""
    if ping and DISCORD_USER_ID:
        text = f"<@{DISCORD_USER_ID}> {text}"
    payload = {"content": text[:1900]}
    try:
        if APPLY_CHANNEL_ID and BOT_TOKEN:
            api = "https://discord.com/api/v10"
            headers = {"Authorization": f"Bot {BOT_TOKEN}"}
            if attachment and os.path.exists(attachment[0]):
                with open(attachment[0], "rb") as f:
                    resp = requests.post(
                        f"{api}/channels/{APPLY_CHANNEL_ID}/messages", headers=headers,
                        data={"payload_json": json.dumps(payload)},
                        files={"files[0]": (attachment[1], f, "application/pdf")}, timeout=TIMEOUT,
                    )
            else:
                resp = requests.post(f"{api}/channels/{APPLY_CHANNEL_ID}/messages",
                                     headers=headers, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            guild = requests.get(f"{api}/channels/{APPLY_CHANNEL_ID}", headers=headers,
                                 timeout=TIMEOUT).json().get("guild_id")
            msg_id = resp.json().get("id")
            return f"https://discord.com/channels/{guild}/{APPLY_CHANNEL_ID}/{msg_id}" if guild and msg_id else None
        if WEBHOOK_URL:
            requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT).raise_for_status()
    except (requests.RequestException, ValueError) as e:
        logging.error(f"Discord notify failed: {e}")
    return None


def notify(packet):
    needs = [q["label"] for q in packet["questions"] if q["needs_you"]]
    drafted = sum(1 for q in packet["questions"] if q["answer"] not in (None, "", []))
    post_discord(
        f"📦 **Packet ready:** {packet['title']} at {packet['company']}\n"
        f"{drafted} answers drafted, resume {'✅' if 'resume' in packet['files'] else '❌'}, "
        f"cover letter {'✅' if 'cover_letter' in packet['files'] else '❌'}\n"
        + (f"❓ Needs you: {'; '.join(n[:80] for n in needs[:6])}\n" if needs else "")
        + f"<{packet['form_url']}>"
    )


def prep(conn, row):
    job_url, title, company, ats, board = row
    job = {"job_url": job_url, "title": title, "company": company}
    set_status(conn, job_url, "prepping")
    try:
        form = resolve_form(job_url, ats, board)
        job["title"] = title or form.get("title", "")
        job["company"] = company or form.get("company", "")
        conn.execute("UPDATE apply_queue SET title = ?, company = ? WHERE job_url = ?",
                     (job["title"], job["company"], job_url))
        import tailor  # lazy: tailor configures logging on import
        resume_pdf, cover_pdf, _ = tailor.tailor(job_url, job_description=form.get("description") or None)
        answers, unknown = draft_answers(job, form)
        pdir, packet = write_packet(job, form, answers, unknown, resume_pdf, cover_pdf)
        set_status(conn, job_url, "ready", packet_dir=pdir,
                   note=f"{len(unknown)} need you" if unknown else "complete")
        notify(packet)
        logging.info(f"Packet ready: {job['title']} @ {job['company']} -> {pdir}")
        return pdir
    except Exception as e:
        logging.exception(f"Prep failed for {job_url}")
        set_status(conn, job_url, "failed", note=str(e)[:300])
        return None


def prep_pending(limit):
    conn = db_conn()
    rows = conn.execute(
        """SELECT job_url, title, company, ats, board FROM apply_queue
           WHERE status = 'queued' ORDER BY fit_score DESC, date_added LIMIT ?""", (limit,),
    ).fetchall()
    done = [prep(conn, r) for r in rows]
    conn.close()
    return done


def packet_resume(packet_dir):
    """(path, filename) of the resume in a packet, or None."""
    try:
        with open(os.path.join(packet_dir, "packet.json")) as f:
            name = json.load(f).get("files", {}).get("resume")
    except (OSError, TypeError, ValueError):
        return None
    return (os.path.join(packet_dir, name), name) if name else None


def ready_packets(conn):
    """[{job_url, company, title, packet}] for every packet waiting to be submitted, where
    packet is the folder name under output/packets (what the Mac copies)."""
    rows = conn.execute("""SELECT job_url, company, title, packet_dir, note FROM apply_queue
                           WHERE status = 'ready' ORDER BY date_updated""").fetchall()
    return [{"job_url": u, "company": c, "title": t, "packet": os.path.basename(d or ""), "note": n}
            for u, c, t, d, n in rows]


def mark_submitted(job_url):
    import sheets_sync
    conn = db_conn()
    row = conn.execute("SELECT title, company, packet_dir FROM apply_queue WHERE job_url = ?",
                       (job_url,)).fetchone()
    title, company, packet_dir = row if row else ("", "", None)
    set_status(conn, job_url, "submitted")
    conn.execute(
        """INSERT OR REPLACE INTO applications (job_url, title, company, status, date_added, notes)
           VALUES (?, ?, ?, 'applied', ?, 'submitted from packet')""",
        (job_url, title, company, _now()),
    )
    conn.commit()
    conn.close()

    resume = packet_resume(packet_dir) if packet_dir else None
    link = post_discord(
        f"✅ **Applied:** {title} at {company}\n"
        f"Submitted {datetime.now().strftime('%a %b %-d, %-I:%M %p')}"
        + (f" with {resume[1]}" if resume else "") + f"\n<{job_url}>",
        ping=True, attachment=resume,
    )
    if resume:
        record_tailored(job_url, resume[1], link)
    return sheets_sync.record_safe(company, title, job_url, "applied",
                                   f"submitted from packet {datetime.now().strftime('%-m/%-d/%Y')}",
                                   resume=resume_used(job_url))


def main():
    logging.basicConfig(
        filename=os.getenv("APPLY_LOG", os.path.join(SCRIPT_DIR, "apply_prep.log")),
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--pending", type=int, help="prep up to N queued jobs")
    p.add_argument("--url", help="enqueue and prep one job now")
    p.add_argument("--list", action="store_true")
    p.add_argument("--submitted", help="mark a job submitted")
    p.add_argument("--skip", help="drop a job from the queue")
    p.add_argument("--notify", help="post a message to #applications (used by the Mac watcher)")
    p.add_argument("--ping", action="store_true", help="with --notify: mention the user")
    p.add_argument("--ready-json", action="store_true", help="ready packets as JSON (for the watcher)")
    args = p.parse_args()
    conn = db_conn()

    if args.url:
        ats = detect_ats(args.url)
        board = greenhouse_ids(args.url)[0] if ats == "greenhouse" else ""
        conn.execute("DELETE FROM apply_queue WHERE job_url = ? AND status IN ('failed', 'skipped')", (args.url,))
        enqueue(conn, {"job_url": args.url, "site": ats, "board": board})
        print(prep(conn, (args.url, "", "", ats, board)) or "failed, see log")
    elif args.pending:
        print("\n".join(d or "failed" for d in prep_pending(args.pending)) or "nothing queued")
    elif args.list:
        for r in conn.execute("SELECT status, company, title, job_url, note FROM apply_queue ORDER BY date_added"):
            print(" | ".join(str(x or "") for x in r))
    elif args.submitted:
        print(mark_submitted(args.submitted) or "marked submitted")
    elif args.skip:
        set_status(conn, args.skip, "skipped")
        print("skipped")
    elif args.notify:
        print(post_discord(args.notify, ping=args.ping) or "posted")
    elif args.ready_json:
        print(json.dumps(ready_packets(conn)))
    conn.close()


if __name__ == "__main__":
    main()
