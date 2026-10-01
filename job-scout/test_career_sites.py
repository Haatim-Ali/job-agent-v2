"""
Tests for career_sites.py title matching / date parsing and scraper.quick_filter.
Run with: python -m pytest job-scout/test_career_sites.py -v
"""

import json
import os
from datetime import datetime, timezone

import pytest

import career_sites as cs

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "scraper_config.json")))


def _match(title):
    return cs.title_matches(title, _CFG["role_keywords"], _CFG["intern_keywords"], _CFG["career_site_exclude_titles"])


class TestTitleMatches:
    @pytest.mark.parametrize("title", [
        "Software Engineering Intern (2027 Start) - Winter",
        "[Summer 2027] Software Engineer Intern",
        "Machine Learning Internship",
        "Software Development Engineer Intern - Summer 2027 (USA)",
        "Data Science Interns",
        "Product Manager Intern, Summer 2027",
    ])
    def test_engineering_internships_match(self, title):
        assert _match(title)

    @pytest.mark.parametrize("title", [
        "Internal Communications, Research and Product",
        "Applied AI Engineer, Government, International",
        "Software Engineer - Database Engine Internals",
    ])
    def test_intern_substring_does_not_match(self, title):
        assert not _match(title)

    @pytest.mark.parametrize("title", [
        "Senior Software Engineer Intern Mentor",
        "PhD Machine Learning Intern",
        "Sales Intern",
        "Recruiting Coordinator Intern",
    ])
    def test_excluded_titles(self, title):
        assert not _match(title)


class TestParseDt:
    def test_iso_with_offset(self):
        assert cs._parse_dt("2026-09-24T17:24:36-04:00") == datetime(2026, 9, 24, 21, 24, 36, tzinfo=timezone.utc)

    def test_iso_z(self):
        assert cs._parse_dt("2026-09-22T22:06:11Z").tzinfo is not None

    def test_epoch_ms(self):
        assert cs._parse_dt(0) == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_amazon_format(self):
        assert cs._parse_dt("September 24, 2026") == datetime(2026, 9, 24, tzinfo=timezone.utc)

    def test_garbage(self):
        assert cs._parse_dt("yesterday") is None
        assert cs._parse_dt(None) is None


class TestHtmlToText:
    def test_double_escaped_greenhouse_content(self):
        assert cs._html_to_text("&lt;p&gt;Build things&lt;/p&gt;") == "Build things"


class TestQuickFilter:
    @pytest.fixture(autouse=True)
    def _scraper(self, monkeypatch):
        import logging
        monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
        import scraper
        self.qf = scraper.quick_filter

    def test_hourly_internship_pay_passes(self):
        # Regression: $50/hr used to be compared against a $40k yearly floor and rejected
        assert self.qf({"title": "SWE Intern", "company": "Stripe", "min_amount": 45, "max_amount": 50, "interval": "hourly"})[0]

    def test_low_hourly_pay_rejected(self):
        assert not self.qf({"title": "SWE Intern", "company": "Acme", "min_amount": 15, "max_amount": 18, "interval": "hourly"})[0]

    def test_low_yearly_pay_rejected(self):
        assert not self.qf({"title": "Engineer", "company": "Acme", "min_amount": 20000, "max_amount": 30000, "interval": "yearly"})[0]

    def test_no_pay_listed_passes(self):
        assert self.qf({"title": "SWE Intern", "company": "Stripe"})[0]

    def test_excluded_company_rejected(self):
        assert not self.qf({"title": "AI Engineer Intern", "company": "Jobright.ai"})[0]


class TestClaudeUnavailable:
    """Regression: when API credits ran out, every job was marked seen as 'rejected' and lost."""

    @pytest.fixture
    def scraper(self, monkeypatch, tmp_path):
        import logging
        monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
        import scraper
        monkeypatch.setattr(scraper, "DB_PATH", str(tmp_path / "jobs.db"))
        monkeypatch.setattr(scraper, "API_ALERT_MARKER", str(tmp_path / ".last_api_alert"))
        monkeypatch.setattr(scraper, "_claude_unavailable", None)
        return scraper

    def _job(self, n):
        return {"title": f"SWE Intern {n}", "company": f"Co{n}", "job_url": f"https://x/{n}",
                "description": "Build things.", "site": "greenhouse"}

    def test_credit_error_leaves_jobs_unseen_and_alerts_once(self, scraper, monkeypatch):
        calls, alerts = [], []

        class FakeMessages:
            def create(self, **kw):
                calls.append(1)
                raise Exception("Error code: 400 - Your credit balance is too low to access the Anthropic API.")

        class FakeClient:
            def __init__(self, **kw):
                self.messages = FakeMessages()

        monkeypatch.setattr(scraper.anthropic, "Anthropic", FakeClient)
        monkeypatch.setattr(scraper, "notify_api_problem", lambda e: alerts.append(e))
        monkeypatch.setattr(scraper, "send_discord_alert", lambda *a, **k: None)
        conn = scraper.init_db()

        alerted = scraper.process_jobs([self._job(1), self._job(2), self._job(3)], conn, from_career_site=True)

        assert alerted == 0
        assert len(calls) == 1          # stops calling Claude after the account error
        assert len(alerts) == 1
        for n in (1, 2, 3):
            assert not scraper.is_seen(conn, f"https://x/{n}")   # kept for retry

    def test_score_uses_structured_output_and_clamps(self, scraper, monkeypatch):
        sent = {}

        class FakeMessages:
            def create(self, **kw):
                sent.update(kw)

                class R:
                    stop_reason = "end_turn"
                    content = [type("C", (), {"type": "text", "text":
                        '{"relevant": true, "reason": "fits", "fit_score": 14, '
                        '"experience_required": "0-1 years", "location_ok": true}'})()]
                return R()

        class FakeClient:
            def __init__(self, **kw):
                self.messages = FakeMessages()

        monkeypatch.setattr(scraper.anthropic, "Anthropic", FakeClient)
        result = scraper.claude_relevance_check(self._job(5))
        assert result["fit_score"] == 10 and result["relevant"]
        fmt = sent["output_config"]["format"]
        assert fmt["type"] == "json_schema" and fmt["schema"]["required"][0] == "relevant"

    def test_one_off_parse_failure_is_still_marked_seen(self, scraper, monkeypatch):
        class FakeMessages:
            def create(self, **kw):
                class R:
                    stop_reason = "end_turn"
                    content = [type("C", (), {"type": "text", "text": "not json"})()]
                return R()

        class FakeClient:
            def __init__(self, **kw):
                self.messages = FakeMessages()

        monkeypatch.setattr(scraper.anthropic, "Anthropic", FakeClient)
        conn = scraper.init_db()
        scraper.process_jobs([self._job(9)], conn, from_career_site=True)
        assert scraper.is_seen(conn, "https://x/9")


class TestNewSources:
    def test_workday_posted(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        assert cs._workday_posted("Posted Today", now) == now
        assert cs._workday_posted("Posted Yesterday", now).day == 30
        assert cs._workday_posted("Posted 3 Days Ago", now).day == 28
        assert (now - cs._workday_posted("Posted 30+ Days Ago", now)).days == 30
        assert cs._workday_posted("", now) is None

    @pytest.mark.parametrize("url, expected", [
        ("https://job-boards.greenhouse.io/truveta/jobs/5659745004", ("greenhouse", "truveta")),
        ("https://boards.greenhouse.io/neuralink/jobs/6083322003", ("greenhouse", "neuralink")),
        ("https://jobs.lever.co/palantir/774cf5c9-bf6a/apply", ("lever", "palantir")),
        ("https://jobs.ashbyhq.com/dryft/3f1c261d/application", ("ashby", "dryft")),
        ("https://jobs.smartrecruiters.com/RESPECInc/744000107100902", ("smartrecruiters", "RESPECInc")),
        ("https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/McLean-VA/Intern_R1",
         ("workday", "capitalone|wd12|Capital_One")),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x",
         ("workday", "nvidia|wd5|NVIDIAExternalCareerSite")),
        ("https://careers.tesla.com/job/123", ("other", "")),
    ])
    def test_ats_from_url(self, url, expected):
        assert cs._ats_from_url(url) == expected

    def test_simplify_filters_and_maps(self, monkeypatch):
        listings = [
            {"active": True, "is_visible": True, "title": "SWE Intern", "company_name": "Truveta",
             "url": "https://job-boards.greenhouse.io/truveta/jobs/1", "locations": ["Seattle, WA"],
             "date_posted": 1790812800, "terms": ["Summer 2027"], "degrees": ["Bachelor's", "Master's"],
             "category": "Software", "sponsorship": "Other"},
            {"active": False, "title": "Closed", "url": "x"},
            {"active": True, "is_visible": True, "title": "PhD Intern", "url": "y", "degrees": ["PhD"]},
            {"active": True, "is_visible": True, "title": "Tesla Intern", "company_name": "Tesla",
             "url": "https://careers.tesla.com/job/1", "date_posted": 1790812800},
        ]

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return listings

        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: Resp())
        jobs = cs.fetch_simplify()
        assert [(j["title"], j["site"], j["board"]) for j in jobs] == [
            ("SWE Intern", "greenhouse", "truveta"), ("Tesla Intern", "simplify", "")]
        assert jobs[0]["from_simplify"] and jobs[0]["date_posted"].year == 2026
        assert "Summer 2027" in jobs[0]["description"]

    def test_parse_dt_epoch_seconds_and_ms(self):
        assert cs._parse_dt(1790812800).year == 2026
        assert cs._parse_dt(1790812800000).year == 2026


def test_new_grad_titles_are_not_internships():
    # Regression: "university" counted as an internship keyword, so Pinterest's full-time
    # "University Grad Software Engineer 2027 (USA)" got an application packet
    assert not _match("University Grad Software Engineer 2027 (USA)")
    assert _match("Software Engineer Intern 2027 (USA)")
