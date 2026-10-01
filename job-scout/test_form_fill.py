"""
Tests for form_fill.py: building the browser fill plan from a packet.
Run with: python -m pytest job-scout/test_form_fill.py -v
"""

import json

import form_fill as ff


def _q(key, label, qtype, answer=None, required=True, options=None):
    return {"key": key, "label": label, "type": qtype, "required": required,
            "options": options or [], "answer": answer, "needs_you": False}


PACKET = {
    "form_url": "https://job-boards.greenhouse.io/waymo/jobs/8238525", "ats": "greenhouse",
    "files": {"resume": "Haatim_Ali_Waymo_Resume.pdf", "cover_letter": "Haatim_Ali_Waymo_Cover_Letter.pdf"},
    "packet_dir": "/p",
    "questions": [
        _q("first_name", "First Name", "text", "Haatim"),
        _q("email", "Email", "text", "haatimta@gmail.com"),
        _q("resume", "Resume/CV", "file"),
        _q("cover_letter", "Cover Letter", "file", required=False),
        _q("question_1", "Why Waymo?", "textarea", "Because of the Labeling team."),
        _q("question_2", "Do you require work authorization?", "select", "No", options=["Yes", "No", "Unknown"]),
        _q("question_3", "Please review and acknowledge our Candidate Privacy Policy", "select",
           "I acknowledge that I have read it.", options=["I acknowledge that I have read it."]),
        _q("question_4", "GPA", "text", None),
        _q("75903", "Race (choose all that apply)", "multiselect", ["Asian"], options=["Asian", "White"]),
        _q("country", "Country", "select", "United States", options=["United States"]),
        _q("candidate-location", "Location (City)", "location", "San Jose, CA"),
    ],
}


def _plan():
    return ff.build_plan(PACKET, 7, email="you@school.edu")


def test_greenhouse_uses_embed_form():
    assert _plan()["url"] == "https://job-boards.greenhouse.io/embed/job_app?for=waymo&token=8238525"


def test_text_fields_in_one_js_call_with_email_override():
    actions = _plan()["actions"]
    js = [a for a in actions if a["name"] == "javascript_tool"][0]["input"]["text"]
    vals = json.loads(js.rsplit("__f.text = ", 1)[1].rsplit(";\nJSON", 1)[0])
    assert actions[-1]["input"]["text"] == "JSON.stringify(__f.setText(__f.text))"
    assert vals == {"first_name": "Haatim", "email": "you@school.edu",
                    "question_1": "Because of the Labeling team."}


def test_dropdowns_pick_exact_option():
    texts = [a["input"].get("text", "") for a in _plan()["actions"]]
    assert any('"question_2"' in t for t in texts)
    assert 'await __f.pick("No")' in texts
    assert 'await __f.pick("Asian")' in texts
    assert 'await __f.pick("Fremont, California, United States")' in texts


def test_agreements_are_held_back_for_approval():
    plan = _plan()
    assert [a["label"] for a in plan["agreements"]] == ["Please review and acknowledge our Candidate Privacy Policy"]
    assert plan["agreements"][0]["actions"]
    assert not any("question_3" in a["input"].get("text", "") for a in plan["actions"])


def test_files_and_manual():
    plan = _plan()
    assert plan["files"] == [{"kind": "resume", "path": "/p/Haatim_Ali_Waymo_Resume.pdf"},
                             {"kind": "cover_letter", "path": "/p/Haatim_Ali_Waymo_Cover_Letter.pdf"}]
    assert plan["manual"] == ["GPA"]


def test_verify_checks_fields_but_not_country_code():
    expect = json.loads(_plan()["verify"]["input"]["text"][len("__f.verify("):-1])
    assert expect["email"] == "you@school.edu" and expect["75903"] == ["Asian"]
    assert "country" not in expect and "question_3" not in expect


def test_hispanic_answered_before_race():
    packet = {**PACKET, "questions": PACKET["questions"] + [
        _q("race", "Race", "select", "Two or More Races", options=["Two or More Races"])]}
    texts = [a["input"].get("text", "") for a in ff.build_plan(packet, 7, email="x@y.z")["actions"]]
    assert texts.index('__f.open("hispanic_ethnicity")') < texts.index('__f.open("race")')


def test_navigate_comes_first():
    assert _plan()["actions"][0] == {"name": "navigate", "input": {"tabId": 7, "url": _plan()["url"]}}


def test_non_greenhouse_plans_text_only():
    packet = {**PACKET, "ats": "lever", "form_url": "https://jobs.lever.co/x/y/apply"}
    plan = ff.build_plan(packet, 7, email="you@school.edu")
    assert plan["url"] == "https://jobs.lever.co/x/y/apply"
    assert "Do you require work authorization? => No" in plan["manual"]


def test_application_email_read_from_yaml(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text('# x\nemail: "you@school.edu"\nlegal_name_on_government_id: "H"\n')
    assert ff.application_email(str(p)) == "you@school.edu"
    assert ff.application_email(str(tmp_path / "missing.yaml")) is None
