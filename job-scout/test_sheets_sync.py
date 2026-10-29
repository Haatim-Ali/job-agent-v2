"""
Tests for sheets_sync.py against an in-memory fake spreadsheet (no Google API calls).
Run with: python -m pytest job-scout/test_sheets_sync.py -v
"""

import re

import pytest

import sheets_sync as ss

HEADER = ss.HEADER
STATUS_BLUE = ss.STATUS_COLORS["Applied"][1]


class FakeWorksheet:
    def __init__(self, title, rows):
        self.title = title
        self.rows = [list(r) for r in rows]
        self.sorted = []

    def get_all_values(self, **kwargs):
        return [list(r) for r in self.rows]

    def _set(self, row, col, value):
        while len(self.rows) < row:
            self.rows.append([])
        r = self.rows[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def update(self, range_name, values, **kwargs):
        col, row = re.match(r"([A-Z])(\d+)", range_name).groups()
        for offset, v in enumerate(values[0]):
            self._set(int(row), ord(col) - 64 + offset, v)

    def append_row(self, values, **kwargs):
        self.rows.append(list(values))

    def sort(self, *specs, range=None):
        self.sorted.append((specs, range))


class FakeSheet:
    def __init__(self, *worksheets):
        self._ws = list(worksheets)

    def worksheets(self):
        return self._ws


def _row(company, role, status="Applied", url="", resume=""):
    return [company, role, status, "10/1/2026", "10/1/2026", "JobAgent", "", ss.posting_cell(url), resume, ""]


def _sheet():
    apps = FakeWorksheet(ss.APPS_TAB, [
        HEADER,
        _row("Stripe", "SWE Intern", url="https://x.com/jobs/1?src=linkedin"),
        _row("NVIDIA", "Deep Learning Intern", url="https://nvidia.wd5.myworkdayjobs.com/job/DL_JR1"),
        _row("Cisco", "Software Engineer", url="https://careers.cisco.com/global/en/job/1"),
    ])
    other = FakeWorksheet("Consulting", [["Company"], ["McKinsey"]])
    return FakeSheet(other, apps), apps, other


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(ss, "today", lambda: "10/29/2026")


def test_normalize_company():
    assert ss.normalize_company("neuro42, Inc.") == "neuro42"
    assert ss.normalize_company("Plaid Technologies") == "plaid"
    assert ss.normalize_company("Adobe Systems") == "adobe"


def test_url_key_ignores_query_and_markdown():
    assert ss.url_key("**https://X.com/jobs/1/?shared_id=abc**") == "x.com/jobs/1"
    assert ss.url_key("") == ""


def test_new_application_appended_with_dates_and_link_then_sorted():
    sheet, apps, other = _sheet()
    msg = ss.record("Celestica", "Student Intern", "https://c.com/j/4", "applied", sheet=sheet)
    assert apps.rows[-1] == ["Celestica", "Student Intern", "Applied", "10/29/2026", "10/29/2026", "JobAgent", "",
                             '=HYPERLINK("https://c.com/j/4", "Open ↗")', "", ""]
    assert apps.sorted == [(((4, "des"), (5, "des")), "A2:J5")]
    assert "added Celestica" in msg
    assert other.rows == [["Company"], ["McKinsey"]]  # other tabs untouched


def test_second_role_at_same_company_is_its_own_row():
    sheet, apps, _ = _sheet()
    msg = ss.record("Stripe", "ML Intern", "https://x.com/jobs/2", "applied", sheet=sheet)
    assert [r[1] for r in apps.rows if r[0] == "Stripe"] == ["SWE Intern", "ML Intern"]
    assert "2 roles there now" in msg


def test_status_update_by_link_even_with_different_query_string():
    sheet, apps, _ = _sheet()
    msg = ss.record("Stripe", "SWE Intern", "https://x.com/jobs/1?utm=email", "rejected", sheet=sheet)
    assert apps.rows[1][2] == "Rejected"
    assert apps.rows[1][4] == "10/29/2026"
    assert apps.rows[1][3] == "10/1/2026"  # applied date kept
    assert "row 2: status → Rejected" in msg


def test_status_update_by_company_and_role():
    sheet, apps, _ = _sheet()
    ss.record("Stripe, Inc.", "swe  intern", "", "interview", sheet=sheet)
    assert apps.rows[1][2] == "Interviewing"


def test_logging_same_application_twice_does_not_duplicate():
    sheet, apps, _ = _sheet()
    msg = ss.record("NVIDIA", "Deep Learning Intern", "https://nvidia.wd5.myworkdayjobs.com/job/DL_JR1?shared_id=z",
                    "applied", sheet=sheet)
    assert len(apps.rows) == 4
    assert "already tracked" in msg


def test_unknown_company_named_from_posting_link():
    # Regression: two rows were logged as "Unknown Company" (an NVIDIA and a Cisco posting).
    sheet, apps, _ = _sheet()
    ss.record("Unknown Company", "Consulting Engineer Intern", "https://careers.cisco.com/global/en/job/2**",
              "applied", sheet=sheet)
    assert apps.rows[-1][0] == "Cisco"
    assert apps.rows[-1][7] == '=HYPERLINK("https://careers.cisco.com/global/en/job/2", "Open ↗")'


def test_legacy_logged_from_note_dropped():
    sheet, apps, _ = _sheet()
    ss.record("Glean", "SWE Intern", "https://g.com/1", "applied", "logged from Discord 10/29/2026", sheet=sheet)
    assert apps.rows[-1][9] == ""
    ss.record("Plaid", "SWE Intern", "https://p.com/1", "applied", "Remote; referral", sheet=sheet)
    assert apps.rows[-1][9] == "Remote; referral"


def test_to_apply_has_no_applied_date():
    sheet, apps, _ = _sheet()
    ss.record("ByteDance", "Researcher", "https://b.com/1", "to_apply", sheet=sheet)
    assert apps.rows[-1][2:4] == ["To Apply", ""]


def test_resume_cells():
    assert ss.resume_cell("Haatim_Ali_Stripe_Resume.pdf", "https://discord.com/channels/1/2/3") == \
        '=HYPERLINK("https://discord.com/channels/1/2/3", "Tailored ↗")'
    assert ss.resume_cell("General/default resume") == "Default"


def test_resume_written_on_update_and_untouched_without_one():
    sheet, apps, _ = _sheet()
    ss.record("Stripe", "SWE Intern", "https://x.com/jobs/1", "interview", sheet=sheet)
    assert apps.rows[1][8] == ""
    ss.record("Stripe", "SWE Intern", "https://x.com/jobs/1", "applied", sheet=sheet, resume="R")
    assert apps.rows[1][8] == "R"


def test_missing_applications_tab_is_reported():
    sheet = FakeSheet(FakeWorksheet("Product", [["Company"]]))
    assert ss.record_safe("Adobe", "Intern", "", "applied", sheet=sheet) is None
    with pytest.raises(RuntimeError, match="--layout"):
        ss.record("Adobe", "Intern", "", "applied", sheet=sheet)


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


def test_dashboard_formulas_use_open_ranges_on_applications():
    cells = dict(ss.dashboard_cells())
    assert cells["B6"].startswith("=COUNTIFS('Applications'!C2:C")
    assert all(f"B{11 + i}" in cells for i in range(len(ss.STATUS_ORDER)))
    assert "group by A" in cells["B23"]
    assert "select Col2 " in cells["G23"] and "select Col3 " in cells["L23"]
    assert cells["D12"].startswith("=SPARKLINE(C12") and STATUS_BLUE in cells["D12"]
    # every status in the dropdown has a chip colour
    assert set(ss.STATUS_ORDER) == set(ss.STATUS_COLORS)


# ── Emails → sheet (record_email) ─────────────────────────────────────────────

def _email_sheet():
    apps = FakeWorksheet(ss.APPS_TAB, [
        HEADER,
        _row("NVIDIA", "NVIDIA 2027 Internships: Deep Learning"),
        _row("NVIDIA", "2027 Systems Software Engineering Intern"),
        _row("Stripe", "SWE Intern"),
        _row("IBM", "Consultant Intern", status="Rejected"),
    ])
    return FakeSheet(apps), apps


def test_email_rejection_updates_the_only_role_at_a_company():
    sheet, apps = _email_sheet()
    msg = ss.record_email("Stripe, Inc.", "", "rejected", "11/2/2026", "Rejection email 11/2/2026: not moving forward",
                          sheet=sheet)
    assert apps.rows[3][2:5] == ["Rejected", "10/1/2026", "11/2/2026"]
    assert apps.rows[3][9] == "Rejection email 11/2/2026: not moving forward"
    assert "Applied → Rejected" in msg


def test_email_picks_the_matching_role_among_several():
    sheet, apps = _email_sheet()
    ss.record_email("NVIDIA", "Systems Software Engineering Intern 2027", "assessment", "11/2/2026", "OA", sheet=sheet)
    assert apps.rows[2][2] == "Assessment"
    assert apps.rows[1][2] == "Applied"


def test_email_without_role_at_multi_role_company_is_left_to_haatim():
    sheet, apps = _email_sheet()
    msg = ss.record_email("NVIDIA", "", "rejected", "11/2/2026", "x", sheet=sheet)
    assert msg == "ambiguous: 2 roles at NVIDIA in the sheet"
    assert [r[2] for r in apps.rows[1:3]] == ["Applied", "Applied"]


def test_email_never_moves_a_row_backwards():
    sheet, apps = _email_sheet()
    assert ss.record_email("IBM", "Consultant Intern", "applied", "11/2/2026", "x", sheet=sheet) == "unchanged"
    assert apps.rows[4][2] == "Rejected"
    # an offer still beats a rejection
    ss.record_email("IBM", "Consultant Intern", "offer", "11/3/2026", "y", sheet=sheet)
    assert apps.rows[4][2] == "Offer"


def test_email_for_untracked_application_adds_a_row():
    sheet, apps = _email_sheet()
    msg = ss.record_email("AMD", "Software Engineer Intern/Co-op", "applied", "9/28/2026", "Confirmation", sheet=sheet)
    assert apps.rows[-1][:6] == ["AMD", "Software Engineer Intern/Co-op", "Applied", "9/28/2026", "9/28/2026", "Email"]
    assert apps.sorted and "added AMD" in msg
    ss.record_email("Glean", "", "rejected", "11/2/2026", "x", sheet=sheet)
    assert apps.rows[-1][1:4] == ["Role not named in email", "Rejected", ""]


def test_same_company_tolerates_suffixes_and_longer_names():
    assert ss.same_company("Rivian and Volkswagen Group Technologies", "Rivian and Volkswagen Group")
    assert ss.same_company("Niantic Spatial, Inc.", "Niantic Spatial")
    assert not ss.same_company("HP", "HPE")


def test_confirmation_without_role_at_multi_role_company_is_quiet():
    # Regression: Cisco/Aurora/HP "we received your application" emails asked "which role?" for nothing.
    sheet, apps = _email_sheet()
    assert ss.record_email("NVIDIA", "", "applied", "10/26/2026", "x", sheet=sheet) == "unchanged"


def test_confirmation_for_untracked_role_at_known_company_adds_row():
    sheet, apps = _email_sheet()
    msg = ss.record_email("NVIDIA", "Research Intern, World Models and Synthetic Data", "applied", "10/25/2026", "x",
                          sheet=sheet)
    assert apps.rows[-1][:3] == ["NVIDIA", "Research Intern, World Models and Synthetic Data", "Applied"]
    assert "added NVIDIA" in msg


def test_confirmation_with_different_title_at_single_role_company_matches():
    sheet, apps = _email_sheet()
    assert ss.record_email("Stripe", "Early Adopter Product Builder", "applied", "10/1/2026", "x", sheet=sheet) == "unchanged"
    assert len(apps.rows) == 5


def test_similar_looking_titles_are_different_roles():
    # Regression: GALLO's "IT Data Analyst" rejection was matched to its "IT Software Developer" row.
    sheet, apps = _email_sheet()
    apps.rows.append(["GALLO", "IT Software Developer Internship 2027", "Rejected", "", "10/29/2026", "Email", "", "", "", ""])
    msg = ss.record_email("GALLO", "IT Data Analyst Internship 2027", "rejected", "10/29/2026", "x", sheet=sheet)
    assert "added GALLO" in msg
    assert ss.role_score("2027 Systems Software Engineering Intern", "Systems Software Engineering Intern 2027") == 1


def test_named_untracked_role_at_multi_role_company_adds_row():
    # Regression: IBM assessments for other IBM roles were reported as "which role?" questions.
    sheet, apps = _email_sheet()
    msg = ss.record_email("NVIDIA", "Product Manager Intern 2027", "assessment", "9/2/2026", "x", sheet=sheet)
    assert "added NVIDIA" in msg


def test_reminder_without_role_is_quiet_when_a_row_already_has_that_status():
    sheet, apps = _email_sheet()
    apps.rows[1][2] = "Assessment"
    assert ss.record_email("NVIDIA", "", "assessment", "9/3/2026", "x", sheet=sheet) == "unchanged"
