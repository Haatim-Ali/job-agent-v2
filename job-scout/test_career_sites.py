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


class TestTailorAlerts:
    @pytest.fixture
    def scraper(self, monkeypatch):
        import logging
        monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
        import scraper
        monkeypatch.setattr(scraper, "CHANNEL_ID", "123")
        return scraper

    def test_tailors_each_alert_and_passes_long_descriptions(self, scraper):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            if "--jd-file" in cmd:
                assert open(cmd[cmd.index("--jd-file") + 1]).read().startswith("Build")
            return type("R", (), {"returncode": 0, "stderr": ""})()

        jobs = [{"job_url": "https://x/1", "description": "Build " + "things " * 100},
                {"job_url": "https://x/2", "description": "Terms: Summer 2027"}]   # Simplify-style, no JD
        assert scraper.tailor_alerts(jobs, runner=runner) == 2
        assert calls[0][2:4] == ["https://x/1", "123"] and "--jd-file" in calls[0]
        assert calls[1][2:] == ["https://x/2", "123"]   # tailor.py fetches the posting itself

    def test_failure_is_logged_not_raised(self, scraper):
        def runner(cmd, **kw):
            raise TimeoutError("slow")
        assert scraper.tailor_alerts([{"job_url": "https://x/1"}], runner=runner) == 0

    def test_packets_are_off_by_default(self, scraper):
        assert scraper.APPLICATION_PACKETS is False and scraper.TAILOR_EVERY_ALERT is False   # 👍-only since 2026-10-26


class TestSephoraWatch:
    RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Summer 2027 Merchandising Intern (San Francisco, CA, US, 94105)</title>
        <link>https://jobs.sephora.com/USA/job/San-Francisco-Merchandising-Intern/1/</link>
        <pubDate>Tue, 06 Oct 2026 2:00:00 GMT</pubDate><description>&lt;p&gt;Join us&lt;/p&gt;</description></item>
      <item><title>Seasonal Warehouse Associate (Avon, IN, US, USA_522899)</title>
        <link>https://jobs.sephora.com/USA/job/Avon/2/</link>
        <pubDate>Tue, 06 Oct 2026 2:00:00 GMT</pubDate><description>x</description></item>
    </channel></rss>"""

    def test_successfactors_rss(self, monkeypatch):
        class Resp:
            content = self.RSS

            def raise_for_status(self):
                pass

        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: Resp())
        jobs = cs.fetch_successfactors("Sephora|https://jobs.sephora.com")
        assert jobs[0]["title"] == "Summer 2027 Merchandising Intern"
        assert jobs[0]["location"] == "San Francisco, CA, US, 94105"
        assert jobs[0]["company"] == "Sephora" and jobs[0]["site"] == "successfactors"
        assert jobs[0]["date_posted"].year == 2026 and "Join us" in jobs[0]["description"]

    def test_any_sephora_internship_is_watched_even_non_tech(self):
        kw = ["intern", "internship"]
        assert cs.is_watched_internship({"company": "Sephora", "title": "Summer 2027 Merchandising Intern"}, ["sephora"], kw)
        assert not cs.is_watched_internship({"company": "Sephora", "title": "Seasonal Warehouse Associate"}, ["sephora"], kw)
        assert not cs.is_watched_internship({"company": "Ulta", "title": "Merchandising Intern"}, ["sephora"], kw)
        assert not cs.is_watched_internship({"company": "Sephora", "title": "Internal Audit Manager"}, ["sephora"], kw)

    def test_rss_date(self):
        assert cs._parse_dt("Tue, 06 Oct 2026 2:00:00 GMT").day == 6


class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cs.requests.HTTPError(f"{self.status_code}")


def _ago(hours):
    return (datetime.now(timezone.utc) - cs.timedelta(hours=hours)).isoformat()


class TestMoreBoards:
    """2026-10-31: ~150 more companies, Eightfold (Netflix, Qualcomm), lighter Greenhouse fetch."""

    def test_greenhouse_fetches_descriptions_only_for_fresh_internships(self, monkeypatch):
        urls = []

        def get(url, **kw):
            urls.append(url)
            if url.endswith("/jobs"):
                return _Resp({"jobs": [
                    {"id": 1, "title": "Software Engineer Intern", "first_published": _ago(5),
                     "absolute_url": "https://x/1", "location": {"name": "Hawthorne, CA"}, "company_name": "SpaceX"},
                    {"id": 2, "title": "Software Engineer Intern", "first_published": _ago(500), "absolute_url": "https://x/2"},
                    {"id": 3, "title": "Internal Tools Engineer", "first_published": _ago(5), "absolute_url": "https://x/3"},
                ]})
            return _Resp({"content": "&lt;p&gt;Build rockets&lt;/p&gt;"})

        monkeypatch.setattr(cs.requests, "get", get)
        jobs = cs.fetch_greenhouse("spacex", max_age_hours=72)
        assert [j["job_url"] for j in jobs] == ["https://x/1"]
        assert jobs[0]["description"] == "Build rockets" and jobs[0]["company"] == "SpaceX"
        assert "content" not in str(urls[0]) and urls[1].endswith("/jobs/1")   # list is fetched without bodies

    def test_greenhouse_keeps_posting_when_description_fails(self, monkeypatch):
        def get(url, **kw):
            if url.endswith("/jobs"):
                return _Resp({"jobs": [{"id": 1, "title": "SWE Intern", "first_published": _ago(1), "absolute_url": "https://x/1"}]})
            return _Resp({}, status=500)
        monkeypatch.setattr(cs.requests, "get", get)
        jobs = cs.fetch_greenhouse("b")
        assert len(jobs) == 1 and jobs[0]["description"] == "" and jobs[0]["company"] == "B"

    def test_workday_uses_configured_company_name(self, monkeypatch):
        monkeypatch.setattr(cs.requests, "post", lambda *a, **k: _Resp({"jobPostings": [
            {"title": "Systems Software Intern", "postedOn": "Posted Today", "externalPath": "/job/1", "locationsText": "San Jose, CA"},
            {"title": "International Sales Executive", "postedOn": "Posted Today", "externalPath": "/job/2"}]}))
        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp({"jobPostingInfo": {"jobDescription": "<p>Hi</p>"}}))
        jobs = cs.fetch_workday("sec|wd3|Samsung_Careers|Samsung", pages=1)
        assert [j["title"] for j in jobs] == ["Systems Software Intern"]
        assert jobs[0]["company"] == "Samsung" and jobs[0]["board"] == "sec|wd3|Samsung_Careers"
        assert cs.fetch_workday("nvidia|wd5|Site", pages=1)[0]["company"] == "Nvidia"   # no name: tenant

    def test_unique_workday_prefers_configured_name(self):
        assert cs._unique_workday(["sec|wd3|Samsung_Careers|Samsung", "sec|wd3|Samsung_Careers", "kla|wd1|Search"]) == \
            ["sec|wd3|Samsung_Careers|Samsung", "kla|wd1|Search"]

    def test_eightfold_apply_v2(self, monkeypatch):
        """Netflix: pcsx is disabled (403), the older apply/v2 API works."""
        now = datetime.now(timezone.utc).timestamp()

        def get(url, params=None, **kw):
            if "/pcsx/" in url:
                return _Resp({"message": "PCSX is not enabled"}, status=403)
            if url.endswith("/apply/v2/jobs"):
                return _Resp({"count": 2, "positions": [
                    {"id": 7, "name": "Software Engineer Intern - Summer 2027", "t_create": now,
                     "locations": ["Los Gatos,California,United States of America"],
                     "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/7"},
                    {"id": 8, "name": "Senior Manager, Internal Communications", "t_create": now}]})
            assert url.endswith("/apply/v2/jobs/7")
            return _Resp({"job_description": "<p>Stream things</p>"})

        monkeypatch.setattr(cs.requests, "get", get)
        jobs = cs.fetch_eightfold("Netflix|explore.jobs.netflix.net|netflix.com")
        assert len(jobs) == 1
        j = jobs[0]
        assert j["company"] == "Netflix" and j["site"] == "eightfold" and j["description"] == "Stream things"
        assert j["job_url"] == "https://explore.jobs.netflix.net/careers/job/7" and "Los Gatos" in j["location"]

    def test_eightfold_pcsx(self, monkeypatch):
        """Qualcomm: the pcsx search API, with relative position URLs."""
        now = datetime.now(timezone.utc).timestamp()

        def get(url, params=None, **kw):
            if url.endswith("/pcsx/search"):
                return _Resp({"data": {"count": 1, "positions": [
                    {"id": 5, "name": "FY27 Intern - AI Software Engineering", "postedTs": now,
                     "locations": ["San Diego, California, United States"], "positionUrl": "/careers/job/5"}]}})
            assert url.endswith("/pcsx/position_details") and params["position_id"] == 5
            return _Resp({"data": {"jobDescription": "<p>Chips</p>"}})

        monkeypatch.setattr(cs.requests, "get", get)
        jobs = cs.fetch_eightfold("Qualcomm|careers.qualcomm.com|qualcomm.com")
        assert [(j["title"], j["job_url"], j["description"]) for j in jobs] == [
            ("FY27 Intern - AI Software Engineering", "https://careers.qualcomm.com/careers/job/5", "Chips")]

    def test_fetch_all_runs_eightfold_and_survives_a_failing_board(self, monkeypatch):
        now = datetime.now(timezone.utc)
        job = cs._job("Software Engineer Intern", "Netflix", "Los Gatos, CA", "https://n/1", "", now, "eightfold")
        monkeypatch.setattr(cs, "fetch_eightfold", lambda spec, max_age_hours=72: [job])

        def boom(board, max_age_hours=72):
            raise cs.requests.ConnectionError("down")
        monkeypatch.setattr(cs, "fetch_greenhouse", boom)
        cfg = {"greenhouse": ["broken"], "eightfold": ["Netflix|h|netflix.com"], "simplify": False}
        out = cs.fetch_all(cfg, _CFG["role_keywords"], _CFG["intern_keywords"], _CFG["career_site_exclude_titles"])
        assert [j["job_url"] for j in out] == ["https://n/1"]

    def test_config_has_the_new_companies(self):
        sites = _CFG["career_sites"]
        assert "nintendo" in sites["greenhouse"] and "spacex" in sites["greenhouse"]
        assert any(e.startswith("Netflix|") for e in sites["eightfold"])
        assert all(len(w.split("|")) in (3, 4) for w in sites["workday"])
        assert "United States" in _CFG["locations"]

    def test_eightfold_stops_paging_at_old_postings_and_on_rate_limit(self, monkeypatch):
        now = datetime.now(timezone.utc).timestamp()
        calls = []

        def get(url, params=None, **kw):
            calls.append(params.get("start"))
            if url.endswith("/pcsx/search"):
                if params["start"] == 0 and len(calls) > 1 and calls.count(0) > 1:
                    return _Resp({"data": {"count": 99, "positions": [
                        {"id": i, "name": "SWE Intern", "postedTs": now, "positionUrl": f"/j/{i}"} for i in range(10)]}})
                if params["start"] == 10:
                    return _Resp({}, status=429)
                return _Resp({"data": {"count": 99, "positions": []}})
            return _Resp({"data": {}})

        monkeypatch.setattr(cs.requests, "get", get)
        assert len(cs.fetch_eightfold("Q|h|q.com")) == 10   # page 2 was rate-limited: keep page 1

        def get_old(url, params=None, **kw):
            if url.endswith("/pcsx/search"):
                return _Resp({"data": {"count": 99, "positions": [
                    {"id": 1, "name": "SWE Intern", "postedTs": now - 3600 * 24 * 30, "positionUrl": "/j/1"}]}})
            raise AssertionError("no details for stale postings")
        pages = []
        monkeypatch.setattr(cs.requests, "get", lambda url, params=None, **kw: pages.append(params["start"]) or get_old(url, params))
        assert cs.fetch_eightfold("Q|h|q.com") == [] and pages == [0, 0]   # probe + one page, then stop


class TestEveryBoard:
    """2026-11-01: every company board behind the Simplify list, plus Oracle, Workable, Rippling."""

    def test_board_from_listing(self):
        f = cs.board_from_listing
        assert f("https://job-boards.greenhouse.io/truveta/jobs/1", "Truveta") == ("greenhouse", "truveta")
        assert f("https://sec.wd3.myworkdayjobs.com/Samsung_Careers/job/x", "Samsung") == \
            ("workday", "sec|wd3|Samsung_Careers|Samsung")
        assert f("https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Troy/Intern_R1", "Magna") == \
            ("workday", "magna|wd3|Magna|Magna")
        assert f("https://hdjq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/2026", "Emerson") == \
            ("oracle", "Emerson|hdjq.fa.us2.oraclecloud.com|CX_1")
        assert f("https://apply.workable.com/eluvio/j/ABC123/apply", "Eluvio") == ("workable", "eluvio")
        assert f("https://apply.workable.com/j/ABC123", "X") is None
        assert f("https://ats.rippling.com/foundation-robotics/jobs/6a54", "Foundation") == ("rippling", "foundation-robotics")
        assert f("https://www.tesla.com/careers/search/job/1", "Tesla") is None

    def test_harvest_dedupes_boards_across_listings(self):
        listings = [{"url": "https://jobs.lever.co/zoox/1", "company_name": "Zoox"},
                    {"url": "https://jobs.lever.co/zoox/2", "company_name": "Zoox"},
                    {"url": "https://sec.wd3.myworkdayjobs.com/Samsung_Careers/job/1", "company_name": "Samsung"},
                    {"url": "https://sec.wd3.myworkdayjobs.com/Samsung_Careers/job/2", "company_name": "Samsung Semi"},
                    {"url": "https://www.google.com/about/careers/1", "company_name": "Google"}]
        assert cs.harvest_boards(listings) == {"lever": ["zoox"], "workday": ["sec|wd3|Samsung_Careers|Samsung"]}

    def test_rotation_skips_configured_and_covers_every_board_once(self):
        harvested = {"greenhouse": [f"b{i}" for i in range(40)] + ["stripe"],
                     "workday": ["nvidia|wd5|NVIDIAExternalCareerSite|NVIDIA corp"]}
        cfg = {"greenhouse": ["Stripe"], "workday": ["nvidia|wd5|NVIDIAExternalCareerSite|NVIDIA"], "max_age_hours": 72}
        runs = [cs.rotation_slice(cfg, harvested, 4, now=1800 * k) for k in range(4)]
        picked = [b for r in runs for b in r.get("greenhouse", [])]
        assert sorted(picked) == sorted(f"b{i}" for i in range(40))   # each harvested board exactly once per cycle
        assert not any(r.get("workday") for r in runs)                  # configured (by tenant/site) is never repeated
        assert all(r.get("greenhouse") for r in runs)                   # the work is spread across runs

    def test_fetch_all_adds_harvested_boards(self, monkeypatch):
        listings = [{"url": "https://jobs.ashbyhq.com/tinyco/1", "company_name": "TinyCo", "active": False}]
        monkeypatch.setattr(cs, "download_simplify", lambda url=cs.SIMPLIFY_URL: listings)
        seen = []
        now = datetime.now(timezone.utc)

        def ashby(board):
            seen.append(board)
            return [cs._job("Software Engineer Intern", "TinyCo", "NYC", "https://t/1", "x", now, "ashby", board)]
        monkeypatch.setattr(cs, "fetch_ashby", ashby)
        cfg = {"harvest_simplify_boards": True, "harvest_groups": 1}
        out = cs.fetch_all(cfg, ["software"], ["intern"], [])
        assert seen == ["tinyco"] and [j["job_url"] for j in out] == ["https://t/1"]
        assert cs.fetch_all({"harvest_simplify_boards": False}, ["software"], ["intern"], []) == [] and seen == ["tinyco"]

    def test_fetch_oracle(self, monkeypatch):
        def get(url, params=None, **kw):
            if url.endswith("/recruitingCEJobRequisitions"):
                assert "siteNumber=CX_1" in params["finder"]
                return _Resp({"items": [{"requisitionList": [
                    {"Id": "9", "Title": "Software Engineering Intern", "PostedDate": _ago(2)[:10], "PrimaryLocation": "Austin, TX, United States"},
                    {"Id": "8", "Title": "Production Specialist", "PostedDate": _ago(2)[:10]}]}]})
            assert 'Id="9"' in params["finder"]
            return _Resp({"items": [{"ExternalDescriptionStr": "<p>Build</p>", "ExternalQualificationsStr": "<p>CS</p>"}]})
        monkeypatch.setattr(cs.requests, "get", get)
        jobs = cs.fetch_oracle("Emerson|h.oraclecloud.com|CX_1")
        assert [(j["title"], j["company"], j["description"]) for j in jobs] == [("Software Engineering Intern", "Emerson", "Build\n\nCS")]
        assert jobs[0]["job_url"] == "https://h.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/9"

    def test_fetch_workable(self, monkeypatch):
        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp({"name": "Eluvio", "jobs": [
            {"title": "Software Intern", "city": "Berkeley", "state": "California", "country": "United States",
             "telecommuting": "True", "url": "https://apply.workable.com/j/1", "description": "<p>Video</p>",
             "published_on": "2026-10-30"}]}))
        j = cs.fetch_workable("eluvio")[0]
        assert (j["company"], j["location"], j["description"]) == ("Eluvio", "Berkeley, California, United States (Remote)", "Video")

    def test_fetch_rippling_reads_dates_from_details(self, monkeypatch):
        def get(url, params=None, **kw):
            if url.endswith("/jobs"):
                return _Resp({"totalPages": 1, "items": [
                    {"id": "a", "name": "Robotics Software Intern", "url": "https://ats.rippling.com/fr/jobs/a",
                     "locations": [{"name": "San Francisco, CA"}]},
                    {"id": "b", "name": "Robotics Software Intern", "url": "https://ats.rippling.com/fr/jobs/b"},
                    {"id": "c", "name": "Director, Clinical"}]})
            if url.endswith("/a"):
                return _Resp({"createdOn": _ago(3), "companyName": "Foundation", "description": {"company": "<p>Us</p>", "role": "<p>Robots</p>"}})
            if url.endswith("/b"):
                return _Resp({"createdOn": _ago(900), "description": "old"})
            raise AssertionError("non-internships aren't opened")
        monkeypatch.setattr(cs.requests, "get", get)
        jobs = cs.fetch_rippling("fr")
        assert [(j["job_url"], j["company"], j["description"]) for j in jobs] == [
            ("https://ats.rippling.com/fr/jobs/a", "Foundation", "Us\n\nRobots")]


class TestApple:
    """2026-11-04: jobs.apple.com (Internships team), parsed from the page's embedded JSON."""

    @staticmethod
    def _page(results, total):
        import json
        data = json.dumps({"loaderData": {"search": {"searchResults": results, "totalRecords": total}}})
        return f'<script>window.__staticRouterHydrationData = JSON.parse({json.dumps(data)});</script>'

    def test_fetch_apple_keeps_recent_and_stops_at_old(self, monkeypatch):
        pages = []

        class R:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                pass

        def get(url, params=None, **kw):
            pages.append(params["page"])
            return R(self._page([
                {"postingTitle": "Software Engineering Internships", "positionId": "200", "transformedPostingTitle": "swe",
                 "postDateInGMT": _ago(5), "jobSummary": "Build iOS", "locations": [{"name": "Cupertino"}, {"name": "Austin"}]},
                {"postingTitle": "MBA Internships", "positionId": "201", "transformedPostingTitle": "mba",
                 "postDateInGMT": _ago(900), "locations": []}], total=60))
        monkeypatch.setattr(cs.requests, "get", get)
        jobs = cs.fetch_apple("internships-STDNT-INTRN")
        assert pages == [1]   # the page ended in an old posting, so no page 2
        assert [(j["title"], j["company"], j["location"], j["job_url"], j["description"]) for j in jobs] == [
            ("Software Engineering Internships", "Apple", "Cupertino; Austin",
             "https://jobs.apple.com/en-us/details/200/swe", "Build iOS")]

    def test_fetch_apple_page_without_data_fails_loudly(self, monkeypatch):
        class R:
            text = "<html>blocked</html>"

            def raise_for_status(self):
                pass
        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: R())
        with pytest.raises(ValueError):
            cs.fetch_apple("internships-STDNT-INTRN")
