"""Tests for the friend's read-only channel (shared_channel in scraper_config.json)."""
from datetime import datetime, timedelta, timezone

import pytest

import career_sites as cs


@pytest.fixture
def scraper(monkeypatch, tmp_path):
    import logging
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    import scraper
    monkeypatch.setattr(scraper, "DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setattr(scraper, "API_ALERT_MARKER", str(tmp_path / ".last_api_alert"))
    monkeypatch.setattr(scraper, "_claude_unavailable", None)
    monkeypatch.setattr(scraper, "SHARED_ENABLED", True)
    monkeypatch.setattr(scraper, "_alerted_jobs", [])
    return scraper


def _job(n, title, **kw):
    return {"title": title, "company": f"Co{n}", "job_url": f"https://x/{n}", "description": "Do things.",
            "site": "greenhouse", **kw}


class Recorder:
    def __init__(self, scraper, monkeypatch, main=None, shared=None):
        self.main, self.shared, self.main_calls, self.shared_calls = [], [], [], []
        monkeypatch.setattr(scraper, "send_discord_alert", lambda job, a, **k: self.main.append(job["job_url"]))
        monkeypatch.setattr(scraper, "send_shared_alert", lambda job, r: self.shared.append(job["job_url"]))
        main = main or {"relevant": True, "reason": "fits", "fit_score": 8}
        shared = shared or {"relevant": True, "track": "Data", "reason": "Analyze data"}
        monkeypatch.setattr(scraper, "claude_relevance_check", lambda job: self.main_calls.append(1) or dict(main))
        monkeypatch.setattr(scraper, "shared_relevance_check",
                            lambda job, track: self.shared_calls.append(track) or dict(shared))


class TestSharedTrack:
    @pytest.mark.parametrize("title, track", [
        ("Product Management Intern", "Product / Program Management"),
        ("Associate Product Manager (APM) Intern", "Product / Program Management"),
        ("Technical Program Manager Intern", "Product / Program Management"),
        ("IT Intern - Summer 2027", "Technology & Information"),
        ("Information Systems Intern", "Technology & Information"),
        ("Technology Consulting Intern", "Technology & Information"),
        ("Data Analyst Intern", "Data"),
        ("Data Science Internship", "Data"),
        ("Business Intelligence Intern", "Data"),
        ("Forward Deployed Software Engineer, Intern", "Forward Deployed"),
        ("Solutions Engineer Intern", "Solutions"),
        ("Solutions Architect Intern", "Solutions"),
    ])
    def test_matches(self, scraper, title, track):
        assert scraper.shared_track(title) == track

    @pytest.mark.parametrize("title", [
        "Software Engineer Intern", "Machine Learning Intern", "Security Intern", "Facilities Intern",
        "Digital Marketing Intern", "Database Intern",
    ])
    def test_no_match(self, scraper, title):
        # whole words only: "it" isn't in "security", "data" isn't "database"
        assert scraper.shared_track(title) is None

    def test_off_when_disabled(self, scraper, monkeypatch):
        monkeypatch.setattr(scraper, "SHARED_ENABLED", False)
        assert scraper.shared_track("Data Analyst Intern") is None


class TestRouting:
    def test_shared_only_job_goes_to_friend_channel_only(self, scraper, monkeypatch):
        rec = Recorder(scraper, monkeypatch)
        conn = scraper.init_db()
        alerted = scraper.process_jobs([_job(1, "Data Analyst Intern", shared_only=True)], conn, from_career_site=True)
        assert alerted == 0 and rec.shared == ["https://x/1"] and rec.main == []
        assert rec.main_calls == []          # Haatim's scoring is skipped
        assert scraper.is_seen(conn, "https://x/1")

    def test_off_topic_shared_search_result_is_skipped_without_scoring(self, scraper, monkeypatch):
        rec = Recorder(scraper, monkeypatch)
        conn = scraper.init_db()
        scraper.process_jobs([_job(2, "Software Engineer Intern", shared_only=True)], conn, from_career_site=True)
        assert rec.main_calls == rec.shared_calls == [] and rec.shared == rec.main == []

    def test_job_for_both_goes_to_both(self, scraper, monkeypatch):
        rec = Recorder(scraper, monkeypatch)
        conn = scraper.init_db()
        alerted = scraper.process_jobs([_job(3, "Data Science Intern")], conn, from_career_site=True)
        assert alerted == 1 and rec.main == rec.shared == ["https://x/3"]

    def test_friend_channel_only_when_haatim_filters_it(self, scraper, monkeypatch):
        rec = Recorder(scraper, monkeypatch, main={"relevant": False, "reason": "no", "fit_score": 2})
        conn = scraper.init_db()
        scraper.process_jobs([_job(4, "Product Manager Intern")], conn, from_career_site=True)
        assert rec.main == [] and rec.shared == ["https://x/4"] and scraper.is_seen(conn, "https://x/4")

    def test_friend_check_rejects(self, scraper, monkeypatch):
        rec = Recorder(scraper, monkeypatch, shared={"relevant": False, "track": "none", "reason": "full-time"})
        conn = scraper.init_db()
        scraper.process_jobs([_job(5, "Data Analyst Intern", shared_only=True)], conn, from_career_site=True)
        assert rec.shared == [] and scraper.is_seen(conn, "https://x/5")

    def test_api_error_posts_nothing_and_retries(self, scraper, monkeypatch):
        """Regression guard: if one check fails, the other channel mustn't get it now and again next run."""
        rec = Recorder(scraper, monkeypatch, shared={"relevant": False, "reason": "api error", "api_error": True})
        conn = scraper.init_db()
        scraper.process_jobs([_job(6, "Data Science Intern")], conn, from_career_site=True)
        assert rec.main == rec.shared == [] and not scraper.is_seen(conn, "https://x/6")

    def test_non_track_job_never_checked_for_friend(self, scraper, monkeypatch):
        rec = Recorder(scraper, monkeypatch)
        conn = scraper.init_db()
        scraper.process_jobs([_job(7, "Software Engineer Intern")], conn, from_career_site=True)
        assert rec.shared_calls == [] and rec.main == ["https://x/7"]

    def test_shared_check_validates_track(self, scraper, monkeypatch):
        monkeypatch.setattr(scraper, "ask_json", lambda *a, **k: {"relevant": True, "track": "none", "reason": "x"})
        monkeypatch.setattr(scraper.anthropic, "Anthropic", lambda **kw: None)
        assert not scraper.shared_relevance_check(_job(8, "Data Intern"), "Data")["relevant"]


class TestCareerSites:
    def test_also_keeps_shared_internships_marked_shared_only(self, monkeypatch):
        now = datetime.now(timezone.utc)
        jobs = [{"title": t, "company": "Co", "job_url": f"https://y/{i}", "date_posted": now - timedelta(hours=1)}
                for i, t in enumerate(["Software Engineer Intern", "Solutions Architect Intern",
                                       "Solutions Architect", "Senior Solutions Intern", "Barista Intern"])]
        monkeypatch.setattr(cs, "fetch_greenhouse", lambda board: [dict(j) for j in jobs])
        also = lambda title: "solutions" in title.lower()
        got = cs.fetch_all({"greenhouse": ["co"], "simplify": False}, ["software"], ["intern"], ["senior"], also=also)
        assert [(j["title"], j["shared_only"]) for j in got] == [
            ("Software Engineer Intern", False), ("Solutions Architect Intern", True)]

