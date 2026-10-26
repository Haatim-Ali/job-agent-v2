"""
Tests for resume_tailor.py (no Claude or LaTeX needed).
Run with: python -m pytest job-scout/test_resume_tailor.py -v
"""

import os

import pytest

import resume_tailor as rt

TEMPLATE = open(os.path.join(os.path.dirname(__file__), "ResumeTemplate.tex"), encoding="utf-8").read()
SECTIONS = rt.extract_sections(TEMPLATE)


def test_template_has_no_tagline_or_headline():
    assert "TAGLINE" not in TEMPLATE


def test_template_matches_current_resume_v5():
    assert "AI Explainability and Accountability (AIEA) Lab" in TEMPLATE
    assert "Bachelor of Science in Computer Science" in TEMPLATE
    assert "saraxjain/uplink" not in TEMPLATE
    assert "github.com/Haatim-Ali/uplink" in TEMPLATE
    assert "Technical Skills" in TEMPLATE
    assert "Campus Involvements" not in TEMPLATE and "Interests" not in TEMPLATE


def test_template_disables_ligatures_for_ats():
    # only f-ligatures: disabling all of them prints -- and `` literally under pdfLaTeX
    assert "\\DisableLigatures[f]" in TEMPLATE and "glyphtounicode" in TEMPLATE


def test_every_section_has_its_bullets():
    assert {m: len(b) for m, b in SECTIONS.items()} == {
        "AIEA_BULLETS": 3, "MONTESSORI_BULLETS": 3, "JOBAGENT_BULLETS": 4, "UPLINK_BULLETS": 4, "PATHWISE_BULLETS": 3}


def test_template_bullets_have_no_bold():
    assert all("\\textbf" not in b for bullets in SECTIONS.values() for b in bullets)


def test_no_edits_reproduces_template_exactly():
    result, sources, changes = rt.apply_edits(SECTIONS, {})
    assert rt.render(TEMPLATE, result) == TEMPLATE
    assert changes == []


def test_echoed_unchanged_bullets_reproduce_template_exactly():
    echo = {m: [{"id": f"{m}-{i}", "text": rt.latex_to_marked(b)} for i, b in enumerate(bs)]
            for m, bs in SECTIONS.items()}
    result, _, changes = rt.apply_edits(SECTIONS, echo)
    assert rt.render(TEMPLATE, result) == TEMPLATE
    assert changes == []


def test_marked_roundtrip_keeps_bold_and_escapes():
    for bullets in SECTIONS.values():
        for b in bullets:
            assert rt.marked_to_latex(rt.latex_to_marked(b)) == b


def test_reorder_keeps_resitem_and_bold():
    m = "AIEA_BULLETS"
    edits = {m: [{"id": f"{m}-2", "text": rt.latex_to_marked(SECTIONS[m][2])},
                 {"id": f"{m}-0", "text": rt.latex_to_marked(SECTIONS[m][0])},
                 {"id": f"{m}-1", "text": rt.latex_to_marked(SECTIONS[m][1])}]}
    result, sources, _ = rt.apply_edits(SECTIONS, edits)
    assert result[m] == [SECTIONS[m][2], SECTIONS[m][0], SECTIONS[m][1]]
    tex = rt.render(TEMPLATE, result)
    assert tex.index("head-to-head") < tex.index("CIFAR-10")
    body = tex.split("\\begin{document}")[1]
    assert "\\item " not in body.replace("\\resitem", "")   # regression: plain \item caused loose spacing


def test_dropping_a_bullet_is_ignored():
    m = "MONTESSORI_BULLETS"
    edits = {m: [{"id": f"{m}-1", "text": rt.latex_to_marked(SECTIONS[m][1])}]}
    result, _, changes = rt.apply_edits(SECTIONS, edits)
    assert result[m] == SECTIONS[m]
    assert any(c.get("status") == "ignored reorder" for c in changes)


class TestValidateEdit:
    orig = "Trained a **CNN image classifier** via GPU-backed **Kubernetes** deployments (Nautilus), managing PVCs, Deployments, and Jobs to eliminate idle GPU-node costs"

    def test_light_reword_accepted(self):
        new = "Trained a **CNN image classifier** on GPU-backed **Kubernetes** infrastructure (Nautilus), managing PVCs, Deployments, and Jobs to eliminate idle GPU-node costs"
        assert rt.validate_edit(self.orig, new) is None

    def test_longer_rejected(self):
        new = self.orig + ", accelerating autonomous-driving perception research for the lab"
        assert "too long" in rt.validate_edit(self.orig, new)

    def test_lost_number_rejected(self):
        o = "Implemented and benchmarked **5** RL algorithms against CarRacing-v2"
        assert "lost numbers" in rt.validate_edit(o, "Implemented and benchmarked several RL algorithms against CarRacing-v")

    def test_invented_number_rejected(self):
        o = "Engineered a benchmarking framework across **3+** RL algorithms in parallel"
        assert "introduced numbers" in rt.validate_edit(o, "Engineered a benchmarking framework across **3+** RL algorithms, cutting time 40%")

    def test_removed_all_bold_rejected(self):
        assert "bold" in rt.validate_edit(self.orig, rt.plain(self.orig))

    def test_unbalanced_bold_rejected(self):
        assert "unbalanced" in rt.validate_edit(self.orig, self.orig.replace("**CNN", "CNN", 1))

    def test_bold_added_to_unbolded_bullet_rejected(self):
        o = "Benchmarked 5 RL algorithms on CarRacing-v2"
        assert "bold" in rt.validate_edit(o, "Benchmarked **5** RL algorithms on CarRacing-v2")

    def test_new_em_dash_rejected(self):
        new = self.orig.replace(" managing", " — managing")
        assert rt.validate_edit(self.orig, new) is not None


def test_rejected_edit_keeps_original():
    m = "AIEA_BULLETS"
    bad = rt.latex_to_marked(SECTIONS[m][0]) + ", and much more impressive things besides that"
    result, _, changes = rt.apply_edits(SECTIONS, {m: [{"id": f"{m}-0", "text": bad},
                                                        {"id": f"{m}-1", "text": rt.latex_to_marked(SECTIONS[m][1])},
                                                        {"id": f"{m}-2", "text": rt.latex_to_marked(SECTIONS[m][2])}]})
    assert result[m][0] == SECTIONS[m][0]
    assert changes[0]["status"] == "rejected"


def test_revert_one_restores_largest_growth_in_place():
    m = "AIEA_BULLETS"
    o0 = rt.latex_to_marked(SECTIONS[m][0])
    o2 = rt.latex_to_marked(SECTIONS[m][2])
    edits = {m: [{"id": f"{m}-2", "text": o2.replace("Produced head-to-head", "Produced")},
                 {"id": f"{m}-0", "text": o0.replace("idle GPU-node costs", "all idle GPU-node costs")},
                 {"id": f"{m}-1", "text": rt.latex_to_marked(SECTIONS[m][1])}]}
    result, sources, changes = rt.apply_edits(SECTIONS, edits)
    assert [c["status"] for c in changes] == ["edited", "edited"]
    assert rt.revert_one(result, sources, SECTIONS, changes)
    assert result[m][1] == SECTIONS[m][0]           # the grown bullet is back to original, same position
    assert result[m][0] != SECTIONS[m][2]           # the shortened edit is kept
    assert rt.revert_one(result, sources, SECTIONS, changes)
    assert not rt.revert_one(result, sources, SECTIONS, changes)


def test_changed_pairs_and_drop_rejected():
    m = "JOBAGENT_BULLETS"
    orig2 = rt.latex_to_marked(SECTIONS[m][2])
    edited2 = orig2.replace("rejection reasons", "rejection telemetry reasons")
    rest = [i for i in range(len(SECTIONS[m])) if i != 2]
    edits = {m: [{"id": f"{m}-2", "text": edited2}]
             + [{"id": f"{m}-{i}", "text": rt.latex_to_marked(SECTIONS[m][i])} for i in rest]}
    pairs = rt.changed_pairs(SECTIONS, edits)
    assert [p[0] for p in pairs] == [f"{m}-2"]
    assert "telemetry" in rt.build_verify_prompt(pairs)
    edits = rt.drop_rejected(SECTIONS, edits, {f"{m}-2"})
    result, _, changes = rt.apply_edits(SECTIONS, edits)
    assert result[m][0] == SECTIONS[m][2]      # original wording restored, new order kept
    assert changes == []


def test_changed_pairs_ignores_garbage():
    assert rt.changed_pairs(SECTIONS, {"AIEA_BULLETS": [{"id": "nonsense", "text": "x"}, "bad", {"id": "AIEA_BULLETS-9", "text": "y"}]}) == []


def test_gaps_go_in_a_text_file_not_chat():
    tailor = pytest.importorskip("tailor")
    name, data = tailor.gaps_attachment("Scale AI", "SWE Intern", "GAPS TO PREPARE FOR:\n1. Go")
    assert name == "Haatim_Ali_Scale_AI_Interview_Prep.txt"
    assert data.decode().startswith("SWE Intern @ Scale AI") and "1. Go" in data.decode()


def test_bold_added_to_unbolded_bullet_is_stripped_not_rejected():
    m = "AIEA_BULLETS"
    o1 = rt.latex_to_marked(SECTIONS[m][1])
    new = o1.replace("Benchmarked 5", "Benchmarked **5**")
    edits = {m: [{"id": f"{m}-{i}", "text": new if i == 1 else rt.latex_to_marked(SECTIONS[m][i])} for i in range(3)]}
    result, _, changes = rt.apply_edits(SECTIONS, edits)
    assert result[m][1] == SECTIONS[m][1] and not changes   # bold dropped, nothing else changed


def test_rewrite_target_is_about_half():
    assert rt.rewrite_target(17) == (7, 9)
    assert rt.rewrite_target(1) == (1, 1)


def test_count_reworded_ignores_unchanged_and_bold_only_changes():
    sections = {"A_BULLETS": ["Built X with Python", "Ran Y on GPUs"]}
    same = {"A_BULLETS": [{"id": "A_BULLETS-1", "text": "Ran Y on GPUs"},
                          {"id": "A_BULLETS-0", "text": "Built **X** with Python"}]}
    assert rt.count_reworded(sections, same) == 0
    changed = {"A_BULLETS": [{"id": "A_BULLETS-0", "text": "Built X in Python"},
                             {"id": "A_BULLETS-1", "text": "Ran Y on GPUs"}]}
    assert rt.count_reworded(sections, changed) == 1
    assert rt.count_reworded(sections, {}) == 0


def test_prompt_requires_a_rewrite_count():
    # Regression: v7's result-first rule made Claude return every bullet untouched (NVIDIA, 2026-10-26)
    tpl = open(os.path.join(os.path.dirname(__file__), "ResumeTemplate.tex")).read()
    sections = rt.extract_sections(tpl)
    p = rt.build_prompt(sections, {"role_title": "R", "company": "C"})
    lo, hi = rt.rewrite_target(sum(len(b) for b in sections.values()))
    assert f"{lo} to {hi} of the" in p
    assert "must look identical after tailoring" not in p
    note = rt.build_retry_note(0, sections)
    assert "reworded only 0" in note and f"{lo} to {hi}" in note
