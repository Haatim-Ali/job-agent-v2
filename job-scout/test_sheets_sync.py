"""
Tests for sheets_sync.py against an in-memory fake spreadsheet (no Google API calls).
Run with: python -m pytest job-scout/test_sheets_sync.py -v
"""

import re

import sheets_sync as ss

HEADER = ["Company", "", "Role", "Contact", "", "Link to Posting", "Job Status", "Notes", "Resume Used"]
PLACEHOLDER = ["s"]


class FakeWorksheet:
    def __init__(self, title, rows):
        self.title = title
        self.rows = [list(r) for r in rows]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def _set(self, row, col, value):
        while len(self.rows) < row:
            self.rows.append([])
        r = self.rows[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def update_acell(self, a1, value):
        col, row = re.match(r"([A-Z])(\d+)", a1).groups()
        self._set(int(row), ord(col) - 64, value)

    def update(self, rng, values):
        col, row = re.match(r"([A-Z])(\d+)", rng).groups()
        for offset, v in enumerate(values[0]):
            self._set(int(row), ord(col) - 64 + offset, v)

    def append_row(self, values, **kwargs):
        self.rows.append(list(values))


class FakeSheet:
    def __init__(self, *worksheets):
        self._ws = list(worksheets)

    def worksheets(self):
        return self._ws


def _sheet():
    product = FakeWorksheet("Product", [HEADER, PLACEHOLDER, ["Adobe"], ["Stripe", "", "SWE Intern", "Jane", "", "https://x/1", "Applied"]])
    consulting = FakeWorksheet("Consulting", [HEADER, PLACEHOLDER, ["IBM Consulting"]])
    return FakeSheet(product, consulting), product, consulting


def test_normalize_company():
    assert ss.normalize_company("neuro42, Inc.") == "neuro42"
    assert ss.normalize_company("Plaid Technologies") == "plaid"
    assert ss.normalize_company("Adobe Systems") == "adobe"


def test_fills_existing_company_row_with_blank_role():
    sheet, product, _ = _sheet()
    msg = ss.record("Adobe Systems", "2027 Intern - Software Engineer", "https://x/2", "applied", "note", sheet=sheet)
    assert product.rows[2][:8] == ["Adobe", "", "2027 Intern - Software Engineer", "", "", "https://x/2", "Applied", "note"]
    assert "Product row 3" in msg


def test_status_update_by_link_keeps_contact():
    sheet, product, _ = _sheet()
    ss.record("Stripe", "SWE Intern", "https://x/1", "interview", sheet=sheet)
    assert product.rows[3][6] == "Interviewing"
    assert product.rows[3][3] == "Jane"


def test_status_update_by_company_and_role():
    sheet, product, _ = _sheet()
    ss.record("Stripe", "swe intern", "", "rejected", sheet=sheet)
    assert product.rows[3][6] == "Rejected"


def test_new_role_at_filled_company_appends_row():
    sheet, product, _ = _sheet()
    ss.record("Stripe", "ML Intern", "https://x/3", "applied", sheet=sheet)
    assert product.rows[3][2] == "SWE Intern"
    assert product.rows[-1][:3] == ["Stripe", "", "ML Intern"]


def test_unknown_company_appends_to_first_tab():
    sheet, product, consulting = _sheet()
    ss.record("Celestica", "Student Intern", "https://x/4", "applied", sheet=sheet)
    assert product.rows[-1][0] == "Celestica"
    assert len(consulting.rows) == 3


def test_matches_company_on_other_tab():
    sheet, _, consulting = _sheet()
    ss.record("IBM Consulting", "BT Consultant Intern", "", "applied", sheet=sheet)
    assert consulting.rows[2][2] == "BT Consultant Intern"


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert ss.record("Adobe", "Intern", "", "applied") is None


def test_env_read_at_call_time_not_import_time(monkeypatch):
    # Regression: bot.py imports sheets_sync before load_dotenv(), so settings
    # captured at import time were always empty and sync silently never ran.
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("JOB_TRACKER_SHEET_ID", raising=False)
    assert not ss.enabled()
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "/tmp/key.json")
    monkeypatch.setenv("JOB_TRACKER_SHEET_ID", "abc")
    assert ss.enabled()


def test_record_safe_swallows_errors():
    class Broken:
        def worksheets(self):
            raise RuntimeError("boom")
    assert ss.record_safe("Adobe", "Intern", "", "applied", sheet=Broken()) is None
