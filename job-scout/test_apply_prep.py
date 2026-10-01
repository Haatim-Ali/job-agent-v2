"""
Tests for apply_prep.py: form resolution helpers, answer validation and the queue.
Run with: python -m pytest job-scout/test_apply_prep.py -v
"""

import sqlite3

import pytest

import apply_prep as ap


class TestDetectAts:
    @pytest.mark.parametrize("url, ats", [
        ("https://boards.greenhouse.io/figma/jobs/6143238004?gh_jid=6143238004", "greenhouse"),
        ("https://stripe.com/jobs/search?gh_jid=8128745", "greenhouse"),
        ("https://jobs.lever.co/palantir/774cf5c9-bf6a-4d77-bf60-d50ef1beb1a0", "lever"),
        ("https://jobs.ashbyhq.com/notion/1fc309c8-da20-4ff2-84c7-8b863ece2b0a", "ashby"),
        ("https://www.linkedin.com/jobs/view/123", None),
    ])
    def test_detects(self, url, ats):
        assert ap.detect_ats(url) == ats


class TestIds:
    def test_greenhouse_board_url(self):
        assert ap.greenhouse_ids("https://boards.greenhouse.io/figma/jobs/6143238004?gh_jid=6143238004") == ("figma", "6143238004")

    def test_greenhouse_company_hosted_needs_board(self):
        assert ap.greenhouse_ids("https://stripe.com/jobs/search?gh_jid=8128745", "stripe") == ("stripe", "8128745")
        assert ap.greenhouse_ids("https://stripe.com/jobs/search?gh_jid=8128745") == ("", "8128745")

    def test_greenhouse_id_in_path(self):
        assert ap.greenhouse_ids("https://jobs.dropbox.com/listing/8106224", "dropbox") == ("dropbox", "8106224")

    def test_lever(self):
        assert ap.lever_ids("https://jobs.lever.co/palantir/774cf5c9-bf6a-4d77-bf60-d50ef1beb1a0/apply") == (
            "palantir", "774cf5c9-bf6a-4d77-bf60-d50ef1beb1a0")

    def test_ashby(self):
        assert ap.ashby_ids("https://jobs.ashbyhq.com/notion/1fc309c8-da20-4ff2-84c7-8b863ece2b0a") == (
            "notion", "1fc309c8-da20-4ff2-84c7-8b863ece2b0a")

    def test_resolve_rejects_unknown_site(self):
        with pytest.raises(ValueError):
            ap.resolve_form("https://www.linkedin.com/jobs/view/123")


QUESTIONS = [
    ap._q("first_name", "First Name", "text", True),
    ap._q("resume", "Resume", "file", True),
    ap._q("auth", "Authorized to work?", "select", True, ["Yes", "No"]),
    ap._q("langs", "Languages", "multiselect", False, ["English", "Spanish"]),
    ap._q("gpa", "GPA", "text", True),
    ap._q("nickname", "Preferred name", "text", False),
    ap._q("school", "University", "select", True, [f"U{i}" for i in range(100)]),
]


class TestValidateAnswers:
    def test_keeps_valid_and_flags_missing_required(self):
        answers, flagged = ap.validate_answers(QUESTIONS, {
            "first_name": "Haatim", "auth": "Yes", "langs": ["English"], "nickname": None,
            "school": "University of California, Santa Cruz",
        }, ["gpa"])
        assert answers == {"first_name": "Haatim", "auth": "Yes", "langs": ["English"],
                           "school": "University of California, Santa Cruz"}
        assert flagged == ["gpa"]

    def test_select_answer_not_in_options_is_flagged(self):
        answers, flagged = ap.validate_answers(QUESTIONS, {"first_name": "H", "auth": "Yes, I am"}, [])
        assert "auth" not in answers and "auth" in flagged

    def test_multiselect_filters_bad_options(self):
        answers, _ = ap.validate_answers(QUESTIONS, {"langs": ["English", "Klingon"]}, [])
        assert answers["langs"] == ["English"]

    def test_multiselect_all_bad_is_flagged(self):
        answers, flagged = ap.validate_answers(QUESTIONS, {"langs": "Klingon"}, [])
        assert "langs" not in answers and "langs" in flagged

    def test_unknown_keys_are_ignored(self):
        answers, flagged = ap.validate_answers(QUESTIONS, {"made_up": "x"}, ["made_up"])
        assert "made_up" not in answers and "made_up" not in flagged

    def test_files_are_never_flagged(self):
        _, flagged = ap.validate_answers(QUESTIONS, {}, [])
        assert "resume" not in flagged


class TestPrompt:
    def test_long_option_lists_are_summarized_and_files_dropped(self):
        items = ap._questions_for_prompt(QUESTIONS)
        keys = [i["key"] for i in items]
        assert "resume" not in keys
        school = next(i for i in items if i["key"] == "school")
        assert "options" not in school and "100 options" in school["note"]

    def test_parse_json_handles_fences_and_prose(self):
        assert ap.parse_json('```json\n{"answers": {"a": 1}}\n```') == {"answers": {"a": 1}}
        assert ap.parse_json('Here you go: {"unknown": []} done') == {"unknown": []}
        assert ap.parse_json("not json") == {}


class TestQueue:
    @pytest.fixture
    def conn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ap, "DB_PATH", str(tmp_path / "jobs.db"))
        c = ap.db_conn()
        yield c
        c.close()

    def test_enqueue_only_supported_sites_once(self, conn):
        job = {"job_url": "https://boards.greenhouse.io/figma/jobs/1", "site": "greenhouse",
               "board": "figma", "title": "SWE Intern", "company": "Figma"}
        assert ap.enqueue(conn, job, 8)
        assert not ap.enqueue(conn, job, 8)
        assert not ap.enqueue(conn, {"job_url": "https://linkedin.com/x", "site": "linkedin"})
        assert conn.execute("SELECT status, board FROM apply_queue").fetchall() == [("queued", "figma")]

    def test_set_status_keeps_existing_packet_dir(self, conn):
        ap.enqueue(conn, {"job_url": "u", "site": "lever"})
        ap.set_status(conn, "u", "ready", packet_dir="/p", note="complete")
        ap.set_status(conn, "u", "submitted")
        assert conn.execute("SELECT status, packet_dir, note FROM apply_queue").fetchone() == ("submitted", "/p", "complete")


def test_slugify():
    assert ap.slugify("Figma-Software Engineer Intern (Summer 2027)") == "figma-software-engineer-intern-summer-2027"


def test_strip_dashes_only_touches_long_text():
    essay = "I built Pathwise -- an agent — in 24 hours – and shipped it to production."
    assert ap.strip_dashes(essay) == "I built Pathwise, an agent, in 24 hours, and shipped it to production."
    assert ap.strip_dashes("Spring 2028 -- Fall") == "Spring 2028 -- Fall"
    assert ap.strip_dashes(["a -- b"]) == ["a -- b"]


class TestDiscord:
    @pytest.fixture
    def posts(self, monkeypatch):
        calls = []

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "999", "guild_id": "777"}

        monkeypatch.setattr(ap.requests, "post", lambda url, **kw: calls.append((url, kw)) or Resp())
        monkeypatch.setattr(ap.requests, "get", lambda url, **kw: Resp())
        return calls

    @pytest.fixture
    def channel(self, monkeypatch):
        monkeypatch.setattr(ap, "APPLY_CHANNEL_ID", "123")
        monkeypatch.setattr(ap, "BOT_TOKEN", "tok")
        monkeypatch.setattr(ap, "DISCORD_USER_ID", "42")

    def test_posts_to_applications_channel_as_bot_with_ping(self, posts, channel):
        link = ap.post_discord("✅ Applied", ping=True)
        url, kw = posts[0]
        assert url.endswith("/channels/123/messages")
        assert kw["headers"]["Authorization"] == "Bot tok"
        assert kw["json"]["content"] == "<@42> ✅ Applied"
        assert link == "https://discord.com/channels/777/123/999"

    def test_attachment_is_uploaded(self, posts, channel, tmp_path):
        pdf = tmp_path / "r.pdf"
        pdf.write_bytes(b"%PDF")
        ap.post_discord("hi", attachment=(str(pdf), "Haatim_Ali_Figma_Resume.pdf"))
        kw = posts[0][1]
        assert kw["files"]["files[0]"][0] == "Haatim_Ali_Figma_Resume.pdf"
        assert '"content": "hi"' in kw["data"]["payload_json"]

    def test_falls_back_to_webhook_without_channel(self, posts, monkeypatch):
        monkeypatch.setattr(ap, "APPLY_CHANNEL_ID", None)
        monkeypatch.setattr(ap, "WEBHOOK_URL", "https://hook")
        assert ap.post_discord("hi") is None
        assert posts[0][0] == "https://hook"

    def test_mark_submitted_logs_notifies_and_records_resume(self, posts, channel, tmp_path, monkeypatch):
        import json
        import sheets_sync
        monkeypatch.setattr(ap, "DB_PATH", str(tmp_path / "jobs.db"))
        sheet_calls = []
        monkeypatch.setattr(sheets_sync, "record_safe", lambda *a, **k: sheet_calls.append(k) or "row 5")
        pdir = tmp_path / "packet"
        pdir.mkdir()
        (pdir / "Haatim_Ali_Figma_Resume.pdf").write_bytes(b"%PDF")
        (pdir / "packet.json").write_text(json.dumps({"files": {"resume": "Haatim_Ali_Figma_Resume.pdf"}}))
        conn = ap.db_conn()
        conn.execute("CREATE TABLE applications (job_url TEXT PRIMARY KEY, title TEXT, company TEXT,"
                     " status TEXT, date_added TEXT, notes TEXT)")
        ap.enqueue(conn, {"job_url": "u", "site": "greenhouse", "title": "SWE Intern", "company": "Figma"})
        ap.set_status(conn, "u", "ready", packet_dir=str(pdir))
        conn.close()

        assert ap.mark_submitted("u") == "row 5"
        conn = sqlite3.connect(str(tmp_path / "jobs.db"))
        assert conn.execute("SELECT status FROM apply_queue").fetchone() == ("submitted",)
        assert conn.execute("SELECT company, status FROM applications").fetchone() == ("Figma", "applied")
        text = json.loads(posts[0][1]["data"]["payload_json"])["content"]
        assert "Applied:** SWE Intern at Figma" in text and "Haatim_Ali_Figma_Resume.pdf" in text
        assert sheet_calls[0]["resume"] == (
            '=HYPERLINK("https://discord.com/channels/777/123/999", "Haatim_Ali_Figma_Resume.pdf")')


class TestResumeUsed:
    def test_default_when_nothing_tailored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ap, "DB_PATH", str(tmp_path / "jobs.db"))
        assert ap.resume_used("u") == "General/default resume"

    def test_tailored_resume_link(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ap, "DB_PATH", str(tmp_path / "jobs.db"))
        ap.record_tailored("u", "Haatim_Ali_Figma_Resume.pdf", "https://discord.com/channels/1/2/3")
        assert ap.resume_used("u") == '=HYPERLINK("https://discord.com/channels/1/2/3", "Haatim_Ali_Figma_Resume.pdf")'

    def test_tailored_without_link_is_file_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ap, "DB_PATH", str(tmp_path / "jobs.db"))
        ap.record_tailored("u", "Haatim_Ali_Figma_Resume.pdf")
        assert ap.resume_used("u") == "Haatim_Ali_Figma_Resume.pdf"


class TestOutputFilename:
    @pytest.fixture
    def tailor(self):
        return pytest.importorskip("tailor")

    @pytest.mark.parametrize("company, kind, expected", [
        ("Figma", "resume", "Haatim_Ali_Figma_Resume.pdf"),
        ("Figma", "cover_letter", "Haatim_Ali_Figma_Cover_Letter.pdf"),
        ("Scale AI", "resume", "Haatim_Ali_Scale_AI_Resume.pdf"),
        ("ByteDance (TikTok)", "resume", "Haatim_Ali_ByteDance_TikTok_Resume.pdf"),
        ("", "resume", "Haatim_Ali_Company_Resume.pdf"),
    ])
    def test_names(self, tailor, company, kind, expected):
        assert tailor.output_filename(company, kind) == expected


class TestFindOnAts:
    DESC = " ".join("word" + "abcdefghij"[i // 10] + "abcdefghij"[i % 10] for i in range(60))

    def test_slug_candidates(self):
        assert ap.slug_candidates("Clay")[:4] == ["clay", "claylabs", "clayhq", "clayinc"]
        slugs = ap.slug_candidates("Scale AI, Inc.")
        assert slugs[0] == "scaleai" and "scale-ai" in slugs and "scale" in slugs
        assert ap.slug_candidates("") == []

    def test_same_job_needs_title_and_description(self):
        assert ap.same_job("Software Engineering Intern", self.DESC,
                           "Software Engineering Intern (Summer 2027)", self.DESC)
        other = " ".join("other" + "klmnopqrst"[i // 10] + "klmnopqrst"[i % 10] for i in range(60))
        assert not ap.same_job("Software Engineering Intern", self.DESC, "Software Engineering Intern", other)
        assert not ap.same_job("Software Engineering Intern", self.DESC, "Data Science Intern", self.DESC)

    def test_same_job_without_descriptions_needs_exact_title(self):
        assert ap.same_job("Software Engineering Intern", "", "Software Engineering Intern ", "")
        assert not ap.same_job("Software Engineering Intern", "", "Software Engineer Intern", "")

    def test_finds_job_on_later_slug_and_skips_missing_boards(self):
        calls = []

        def fetch(site):
            def f(slug):
                calls.append((site, slug))
                if site == "ashby" and slug == "claylabs":
                    return [{"title": "Data Intern", "description": self.DESC},
                            {"title": "Software Engineering Intern", "description": self.DESC,
                             "job_url": "https://jobs.ashbyhq.com/claylabs/x", "site": "ashby", "board": "claylabs"}]
                raise RuntimeError("404")
            return f

        job = ap.find_on_ats("Clay", "Software Engineering Intern", self.DESC,
                             fetchers={s: fetch(s) for s in ("greenhouse", "lever", "ashby")})
        assert job["board"] == "claylabs" and job["site"] == "ashby"
        assert ("greenhouse", "clay") in calls and calls[-1] == ("ashby", "claylabs")

    def test_no_match_returns_none(self):
        job = ap.find_on_ats("Clay", "Software Engineering Intern", self.DESC,
                             fetchers={"greenhouse": lambda slug: [{"title": "Sales Intern", "description": self.DESC}]})
        assert job is None

    def test_same_title_in_two_cities_picks_matching_location(self):
        sf = {"title": "Software Engineer Intern (Summer 2027)", "location": "San Francisco, CA", "job_url": "sf"}
        ldn = {"title": "Software Engineer Intern (London, United Kingdom) (Summer 2027)",
               "location": "London, England", "job_url": "ldn"}
        job = ap.find_on_ats("Figma", "Software Engineer Intern", "", "San Francisco, CA",
                             fetchers={"greenhouse": lambda slug: [ldn, sf]})
        assert job["job_url"] == "sf"

    def test_ambiguous_match_is_not_guessed(self):
        a = {"title": "Software Engineer Intern", "location": "Toronto", "job_url": "a"}
        b = {"title": "Software Engineer Intern", "location": "London", "job_url": "b"}
        assert ap.find_on_ats("Figma", "Software Engineer Intern", "", "San Jose, CA",
                              fetchers={"greenhouse": lambda slug: [a, b]}) is None

    def test_season_in_title_breaks_tie(self):
        summer = {"title": "Software Engineer Intern (Summer 2027)", "location": "San Francisco, CA", "job_url": "s"}
        winter = {"title": "Software Engineer Intern (Winter 2027)", "location": "San Francisco, CA", "job_url": "w"}
        job = ap.find_on_ats("Figma", "Software Engineer Intern - Summer 2027", "", "San Francisco",
                             fetchers={"greenhouse": lambda slug: [winter, summer]})
        assert job["job_url"] == "s"
        job = ap.find_on_ats("Figma", "Software Engineer Intern (Summer 2027)", "", "San Francisco",
                             fetchers={"greenhouse": lambda slug: [winter, summer]})
        assert job["job_url"] == "s"


def test_greenhouse_form_includes_demographic_survey(monkeypatch):
    data = {
        "title": "SWE Intern", "company_name": "Waymo", "content": "",
        "questions": [{"label": "First Name", "required": True,
                       "fields": [{"name": "first_name", "type": "input_text", "values": []}]}],
        "demographic_questions": {"questions": [
            {"id": 75902, "label": "Please indicate your gender", "required": True,
             "type": "multi_value_single_select",
             "answer_options": [{"id": 1, "label": "Male"}, {"id": 2, "label": "Female"}]}]},
    }

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    monkeypatch.setattr(ap.requests, "get", lambda *a, **k: Resp())
    form = ap.greenhouse_form("waymo", "8238525")
    gender = next(q for q in form["questions"] if q["key"] == "75902")
    assert gender["type"] == "select" and gender["options"] == ["Male", "Female"] and gender["required"]
