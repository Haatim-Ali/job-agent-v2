"""Tests for email_scan.py with a fake Gmail session and a fake Claude (no network)."""

import base64

import pytest

import email_scan as es
import sheets_sync as ss
from test_sheets_sync import FakeSheet, FakeWorksheet, HEADER, _row


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(msg_id, subject, body, sender="no-reply@myworkday.com", html=False):
    part = {"mimeType": "text/html" if html else "text/plain", "body": {"data": _b64(body)}}
    return {"id": msg_id, "internalDate": "1793664000000",  # 2026-11-02 PT
            "payload": {"mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
                        "parts": [part]}}


class FakeResp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeGmail:
    def __init__(self, messages):
        self.messages = {m["id"]: m for m in messages}
        self.queries = []

    def get(self, url, params=None, timeout=None):
        if url.endswith("/messages"):
            self.queries.append(params["q"])
            return FakeResp({"messages": [{"id": i} for i in reversed(list(self.messages))]})
        return FakeResp(self.messages[url.rsplit("/", 1)[1]])


@pytest.fixture
def fake_claude(monkeypatch):
    answers = {}
    monkeypatch.setattr(es, "classify", lambda client, email: answers[email["id"]])
    return answers


def test_body_text_prefers_plain_and_falls_back_to_html():
    assert es.body_text(_msg("1", "s", "Hello there")["payload"]) == "Hello there"
    html_body = "<html><style>p{}</style><p>We regret&nbsp;to inform</p><br>you</html>"
    assert "We regret\xa0to inform" in es.body_text(_msg("2", "s", html_body, html=True)["payload"])


def test_parse_message_reads_headers_and_pacific_date():
    e = es.parse_message(_msg("9", "Update on your application", "text", sender="NVIDIA <nvidia@myworkday.com>"))
    assert (e["from"], e["subject"], e["date"]) == ("NVIDIA <nvidia@myworkday.com>", "Update on your application", "11/2/2026")


def test_scan_updates_sheet_records_each_email_once_and_reports(tmp_path, fake_claude):
    apps = FakeWorksheet(ss.APPS_TAB, [HEADER, _row("Stripe", "SWE Intern"), _row("NVIDIA", "Deep Learning Intern"),
                                       _row("NVIDIA", "Systems Software Intern")])
    sheet = FakeSheet(apps)
    gmail = FakeGmail([_msg("a", "Your application", "unfortunately"), _msg("b", "Newsletter", "jobs for you"),
                       _msg("c", "NVIDIA update", "not moving forward")])
    fake_claude.update({
        "a": {"kind": "rejection", "company": "Stripe", "role": "", "summary": "Not moving forward"},
        "b": {"kind": "other", "company": "", "role": "", "summary": "Job alert"},
        "c": {"kind": "rejection", "company": "NVIDIA", "role": "", "summary": "Not moving forward"},
    })
    conn = es.db_conn(str(tmp_path / "jobs.db"))
    events = es.scan(client=object(), sess=gmail, sheet=sheet, conn=conn)

    assert "after:2026/08/01" in gmail.queries[0]
    assert apps.rows[1][2] == "Rejected"
    assert [e["company"] for e in events] == ["Stripe", "NVIDIA"]
    assert events[1]["result"].startswith("ambiguous")
    assert conn.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 3

    text = es.summary_message(events)
    assert "❌ **Stripe**: Not moving forward" in text
    assert "2 roles at NVIDIA in the sheet. Which one?" in text

    # second run lists the same range but handles nothing again
    assert es.scan(client=object(), sess=gmail, sheet=sheet, conn=conn) == []
    assert "after:2026/08/01" in gmail.queries[1]


def test_a_failing_message_is_skipped_and_retried_later(tmp_path, monkeypatch):
    # Regression: one error mid-backfill crashed the run and the rest of the inbox was never read.
    apps = FakeWorksheet(ss.APPS_TAB, [HEADER, _row("Stripe", "SWE Intern")])
    calls = []

    def flaky(client, email):
        calls.append(email["id"])
        if email["id"] == "a" and calls.count("a") == 1:
            raise RuntimeError("overloaded")
        return {"kind": "rejection", "company": "Stripe", "role": "", "summary": "No"}
    monkeypatch.setattr(es, "classify", flaky)
    conn = es.db_conn(str(tmp_path / "jobs.db"))
    gmail = FakeGmail([_msg("a", "s", "b"), _msg("b", "s", "b")])
    es.scan(client=object(), sess=gmail, sheet=FakeSheet(apps), conn=conn)
    assert [r[0] for r in conn.execute("SELECT msg_id FROM email_events")] == ["b"]
    es.scan(client=object(), sess=gmail, sheet=FakeSheet(apps), conn=conn)
    assert sorted(r[0] for r in conn.execute("SELECT msg_id FROM email_events")) == ["a", "b"]


def test_dry_run_changes_nothing(tmp_path, fake_claude, capsys):
    apps = FakeWorksheet(ss.APPS_TAB, [HEADER, _row("Stripe", "SWE Intern")])
    fake_claude["a"] = {"kind": "rejection", "company": "Stripe", "role": "", "summary": "No"}
    conn = es.db_conn(str(tmp_path / "jobs.db"))
    es.scan(client=object(), sess=FakeGmail([_msg("a", "s", "b")]), sheet=FakeSheet(apps), conn=conn, dry=True)
    assert apps.rows[1][2] == "Applied"
    assert conn.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 0
    assert "rejection Stripe" in capsys.readouterr().out


def test_no_summary_when_nothing_happened():
    assert es.summary_message([]) is None


def test_missing_token_is_an_auth_error(monkeypatch, tmp_path):
    monkeypatch.setattr(es, "TOKEN_PATH", str(tmp_path / "none.json"))
    with pytest.raises(es.GmailAuthError):
        es.session()


def test_prompt_and_query_cover_the_cases():
    p = es.classify_prompt({"from": "a", "subject": "b", "date": "c", "body": "x" * 5000})
    assert "When unsure, use other" in p and len(p) < 5000
    assert "unfortunately" in es.QUERY and "myworkday.com" in es.QUERY


def test_campus_senders_are_never_applications(tmp_path, fake_claude):
    # Regression: Alpha Kappa Psi rush "interviews" from classmates became tracker rows.
    apps = FakeWorksheet(ss.APPS_TAB, [HEADER])
    fake_claude["a"] = {"kind": "interview", "company": "The Gym", "role": "", "summary": "Interview Fri"}
    conn = es.db_conn(str(tmp_path / "jobs.db"))
    gmail = FakeGmail([_msg("a", "Interview @ The Gym", "x", sender="Rishab <rrvemula@ucsc.edu>")])
    assert es.scan(client=object(), sess=gmail, sheet=FakeSheet(apps), conn=conn) == []
    assert len(apps.rows) == 1
    assert "-from:ucsc.edu" in es.QUERY and "-from:me" in es.QUERY
