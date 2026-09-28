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
