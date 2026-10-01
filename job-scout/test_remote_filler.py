"""
Tests for remote_filler.py's planning (no browser).
Run with: python -m pytest job-scout/test_remote_filler.py -v
"""

import pytest

pytest.importorskip("aiohttp")
import remote_filler as rf  # noqa: E402


def _q(key, label, qtype, answer=None, required=True, options=None):
    return {"key": key, "label": label, "type": qtype, "required": required,
            "options": options or [], "answer": answer, "needs_you": False}


GH = {"ats": "greenhouse", "form_url": "https://job-boards.greenhouse.io/figma/jobs/1", "packet_dir": "/p",
      "files": {"resume": "R.pdf", "cover_letter": "C.pdf"},
      "questions": [
          _q("first_name", "First Name", "text", "Haatim"), _q("email", "Email", "text", "x@gmail.com"),
          _q("resume", "Resume", "file"), _q("cover_letter", "Cover Letter", "file", required=False),
          _q("q1", "Work authorization?", "select", "Yes", options=["Yes", "No"]),
          _q("race", "Race", "select", "Two or More Races", options=["Two or More Races"]),
          _q("q2", "Please acknowledge our privacy policy", "select", "I acknowledge", options=["I acknowledge"]),
          _q("q3", "GPA", "text", None),
          _q("candidate-location", "Location (City)", "location", "Fremont, CA"),
      ]}


def test_greenhouse_plan():
    p = rf.plan(GH, email="you@school.edu")
    assert p["url"] == "https://job-boards.greenhouse.io/embed/job_app?for=figma&token=1"
    assert p["text"] == {"first_name": "Haatim", "email": "you@school.edu"}
    ops = [(s["op"], s.get("key")) for s in p["steps"]]
    assert ops.index(("combo", "hispanic_ethnicity")) < ops.index(("combo", "race"))
    assert ("location", "candidate-location") in ops
    assert p["files"] == [("resume", "/p/R.pdf"), ("cover_letter", "/p/C.pdf")]
    assert p["agreements"] == ["Please acknowledge our privacy policy"]   # never auto-ticked
    assert p["manual"] == ["GPA"]
    assert p["expect"]["candidate-location"] == "Fremont, California, United States"


def test_ashby_choices_and_dates():
    packet = {"ats": "ashby", "form_url": "https://jobs.ashbyhq.com/x/abc/application", "packet_dir": "/p",
              "files": {"resume": "R.pdf"},
              "questions": [_q("_systemfield_resume", "Resume", "file"),
                            _q("u1", "Are you in the Bay Area?", "select", "Yes", options=["Yes", "No"]),
                            _q("u2", "What's your earliest start date?", "text", "Any time after December 30, 2026")]}
    p = rf.plan(packet, email="you@school.edu")
    assert {"op": "choice", "key": "u1", "label": "Are you in the Bay Area?", "value": "Yes"} in p["steps"]
    assert {"op": "date", "key": "u2", "label": "What's your earliest start date?", "value": "12/31/2026"} in p["steps"]
    assert p["files"] == [("_systemfield_resume", "/p/R.pdf")]


@pytest.mark.parametrize("text, expected", [
    ("Any time after December 30, 2026", "12/31/2026"),
    ("March 2, 2027", "03/02/2027"),
    ("ASAP", None),
])
def test_date_value(text, expected):
    assert rf._date_value(text) == expected


def test_confirmation_pattern():
    assert rf.CONFIRMATION.search("Thank you for applying to Figma!")
    assert rf.CONFIRMATION.search("Your application has been submitted.")
    assert not rf.CONFIRMATION.search("Apply for this job")


def test_setup_js_has_helpers_and_no_text():
    js = rf.setup_js()
    assert "window.__f = {" in js and "__f.choice" in js and "__f.text =" not in js
