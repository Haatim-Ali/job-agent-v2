"""
Read Haatim's application inbox (htali@ucsc.edu) and keep the tracker sheet current.

Twice a day (cron) this lists recent recruiting emails through the Gmail API (read-only scope),
asks Claude Haiku what each one is (rejection, assessment, interview, offer, confirmation, or
nothing to act on), writes the result into the Applications tab, and posts a summary in the
actions channel (#reactions). Each message is handled once (table `email_events`).

Setup, once, on the Mac (opens a browser to sign in as htali@ucsc.edu):
  python email_scan.py --auth path/to/oauth_client.json
That writes gmail_token.json (git-ignored); copy it to job-scout/ on the VPS.
The scope is gmail.readonly: this code can't send, delete, label or move mail.

  python email_scan.py            scan new mail (first run reads back to SCAN_SINCE)
  python email_scan.py --dry      classify and print, change nothing
"""

import base64
import html
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "jobs.db")
TOKEN_PATH = os.getenv("GMAIL_TOKEN_JSON") or os.path.join(SCRIPT_DIR, "gmail_token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCAN_SINCE = "2026/08/01"  # every run lists mail since here; messages already handled are skipped
PACIFIC = ZoneInfo("America/Los_Angeles")

# Senders and subjects that recruiting mail comes from. Broad on purpose: Claude decides
# what each message actually is, and anything irrelevant is classified "other".
ATS_SENDERS = ["myworkday.com", "myworkdayjobs.com", "greenhouse.io", "greenhouse-mail.io", "ashbyhq.com",
               "lever.co", "smartrecruiters.com", "icims.com", "jobvite.com", "workable.com", "codesignal.com",
               "hackerrank.com", "wellfound.com", "successfactors.com", "taleo.net", "eightfold.ai",
               "avature.net", "oraclecloud.com", "bamboohr.com", "rippling.com", "deel.com"]
QUERY = ("(from:(" + " OR ".join(ATS_SENDERS) + ") OR subject:(application OR applying OR interview OR "
         "assessment OR candidacy OR \"next steps\" OR internship OR intern) OR unfortunately) "
         "-category:promotions -category:social")

KINDS = ["rejection", "assessment", "interview", "offer", "confirmation", "other"]
KIND_STATUS = {"rejection": "rejected", "assessment": "assessment", "interview": "interview",
               "offer": "offer", "confirmation": "applied"}
KIND_ICON = {"rejection": "❌", "assessment": "📝", "interview": "🎯", "offer": "🎉", "confirmation": "📨"}


# ── Gmail ──────────────────────────────────────────────────────────────────────

class GmailAuthError(Exception):
    """The saved Gmail sign-in is missing, revoked or expired; --auth must be run again."""


def session():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    if not os.path.exists(TOKEN_PATH):
        raise GmailAuthError(f"no Gmail token at {TOKEN_PATH}")
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise GmailAuthError(f"Gmail sign-in expired or revoked: {e}")
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return AuthorizedSession(creds)


def list_ids(sess, query):
    ids, token = [], None
    while True:
        params = {"q": query, "maxResults": 100, **({"pageToken": token} if token else {})}
        data = sess.get(f"{API}/messages", params=params, timeout=30).json()
        if "error" in data:
            raise RuntimeError(f"Gmail list failed: {data['error'].get('message')}")
        ids += [m["id"] for m in data.get("messages", [])]
        token = data.get("nextPageToken")
        if not token:
            return ids


def _b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    return re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def body_text(payload):
    """Plain text of a Gmail message payload: the text/plain part if any, else the HTML as text."""
    plain, rich = [], []

    def walk(part):
        mime, data = part.get("mimeType", ""), part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(html_to_text(_b64(data)))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(plain).strip() or "\n".join(rich).strip()


def parse_message(msg):
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    when = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, timezone.utc).astimezone(PACIFIC)
    return {"id": msg["id"], "from": headers.get("from", ""), "subject": headers.get("subject", ""),
            "date": when.strftime("%-m/%-d/%Y"), "body": body_text(msg.get("payload", {}))}


# ── Classification ─────────────────────────────────────────────────────────────

def classify_prompt(email):
    return f"""This email arrived in the inbox Haatim (a CS student) uses for internship applications.
Say what it means for his applications.

kind:
- rejection: he is not moving forward (including "position filled", "decided to pursue other candidates")
- assessment: an invitation to an online assessment, coding challenge or take-home
- interview: an invitation to schedule or attend an interview or phone screen
- offer: a job offer
- confirmation: confirms an application he submitted ("we received your application")
- other: anything else (job alerts, newsletters, recommendations, reminders to finish an application,
  account emails, career fairs, marketing). When unsure, use other.

company: the hiring company's usual name (e.g. "NVIDIA", not "NVIDIA Corporation via Workday"); "" if none.
role: the exact position title if the email names it, else "".
summary: at most 12 words, e.g. "OA due Nov 3 on HackerRank".

From: {email['from']}
Subject: {email['subject']}
Date: {email['date']}

{email['body'][:3500]}"""


def classify(client, email):
    from claude_json import SCORE_MODEL, STR, ask_json, obj
    schema = obj({"kind": {"type": "string", "enum": KINDS}, "company": STR, "role": STR, "summary": STR})
    return ask_json(client, classify_prompt(email), schema, model=SCORE_MODEL, max_tokens=300,
                    temperature=0, label="email_scan")


# ── Bookkeeping ────────────────────────────────────────────────────────────────

def db_conn(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS email_events (
        msg_id TEXT PRIMARY KEY, date TEXT, sender TEXT, subject TEXT,
        kind TEXT, company TEXT, role TEXT, summary TEXT, result TEXT, scanned_at TEXT)""")
    return conn


def seen(conn, msg_id):
    return conn.execute("SELECT 1 FROM email_events WHERE msg_id = ?", (msg_id,)).fetchone() is not None


def save(conn, email, c, result):
    conn.execute("INSERT OR REPLACE INTO email_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (email["id"], email["date"], email["from"], email["subject"], c["kind"], c["company"],
                  c["role"], c["summary"], result, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()


def summary_message(events):
    """Discord text for this run: one line per email that changed something or needs Haatim."""
    lines = []
    for e in events:
        icon = KIND_ICON.get(e["kind"], "•")
        role = f" — {e['role']}" if e["role"] else ""
        line = f"{icon} **{e['company'] or 'Unknown company'}**{role}: {e['summary']}"
        if e["result"].startswith("ambiguous"):
            line += f"\n  ↳ ❓ {e['result'].split(': ', 1)[1]}. Which one? Set it in the sheet."
        lines.append(line)
    if not lines:
        return None
    return "📬 **Inbox check (htali@ucsc.edu)**\n" + "\n".join(lines)


def scan(client=None, sess=None, sheet=None, conn=None, dry=False):
    """Handle every new recruiting email. Returns the list of events that mattered."""
    import sheets_sync
    sess = sess or session()
    conn = conn or db_conn()
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    # Always list back to SCAN_SINCE (listing is cheap; only unseen messages are fetched), so a
    # run that crashed halfway is finished by the next one.
    query = QUERY + f" after:{SCAN_SINCE}"
    events = []
    for msg_id in reversed(list_ids(sess, query)):  # oldest first, so later news wins
        if seen(conn, msg_id):
            continue
        try:
            email = parse_message(sess.get(f"{API}/messages/{msg_id}", params={"format": "full"}, timeout=30).json())
            c = classify(client, email)
        except Exception as e:  # not saved, so it is retried next run
            logging.warning(f"Inbox check: skipped message {msg_id}: {e}")
            continue
        status = KIND_STATUS.get(c["kind"])
        if not status or not c["company"]:
            result = "ignored"
        elif dry:
            result = "dry run"
        else:
            note = f"{c['kind'].title()} email {email['date']}: {c['summary']}"
            result = sheets_sync.record_email(c["company"], c["role"], status, email["date"], note, sheet=sheet)
        if dry:
            print(email["date"], c["kind"], c["company"], "|", c["role"], "|", c["summary"])
        else:
            save(conn, email, c, result)
        if result not in ("ignored", "unchanged", "dry run"):
            events.append({**c, "result": result})
    return events


def main(argv):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--auth" in argv:
        from google_auth_oauthlib.flow import InstalledAppFlow
        client_file = argv[argv.index("--auth") + 1]
        creds = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES).run_local_server(
            port=0, login_hint="htali@ucsc.edu", prompt="consent")
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        print(f"Saved Gmail sign-in to {TOKEN_PATH}")
        return
    import apply_prep
    try:
        events = scan(dry="--dry" in argv)
    except GmailAuthError as e:
        logging.error(str(e))
        apply_prep.post_discord(f"⚠️ Inbox check can't read htali@ucsc.edu: {e}. "
                                "Re-run `python email_scan.py --auth <client.json>` on the Mac.", ping=True)
        return
    logging.info(f"Inbox check: {len(events)} emails changed the tracker or need you")
    text = summary_message(events)
    if text and "--dry" not in argv:
        ping = any(e["kind"] in ("assessment", "interview", "offer") or e["result"].startswith("ambiguous")
                   for e in events)
        apply_prep.post_discord(text, ping=ping)


if __name__ == "__main__":
    main(sys.argv[1:])
