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
    assert "changed only 0" in note and f"{lo} to {hi}" in note


# ── Bullet bank, projects, coursework, skills (2026-10-26 rebuild) ───────────

BANK, SLOTS, EXTRAS = rt.load_bank(SECTIONS)


def test_bank_puts_defaults_first_and_adds_alternates():
    for m, defaults in SECTIONS.items():
        assert BANK[m][:len(defaults)] == defaults and SLOTS[m] == len(defaults)
    assert len(BANK["UPLINK_BULLETS"]) > SLOTS["UPLINK_BULLETS"]
    assert len(BANK["JOBAGENT_BULLETS"]) > SLOTS["JOBAGENT_BULLETS"]
    assert "Playwright" in EXTRAS


def test_bank_bullets_have_no_em_dash_or_bold():
    for m, bullets in BANK.items():
        for b in bullets:
            assert "**" not in rt.latex_to_marked(b) and "—" not in rt.latex_to_marked(b)


def test_alternate_can_replace_a_default_and_is_reported():
    m = "UPLINK_BULLETS"
    alt = SLOTS[m]          # first alternate
    pick = [alt, 0, 1, 2]
    edits = {m: [{"id": f"{m}-{i}", "text": rt.latex_to_marked(BANK[m][i])} for i in pick]}
    result, sources, changes = rt.apply_edits(BANK, edits, SLOTS)
    assert result[m][0] == BANK[m][alt] and len(result[m]) == SLOTS[m]
    assert {"id": f"{m}-{alt}", "status": "alternate"} in changes
    assert rt.count_reworded(BANK, edits, SLOTS) == 1


def test_wrong_number_of_picks_keeps_defaults():
    m = "UPLINK_BULLETS"
    edits = {m: [{"id": f"{m}-{i}", "text": rt.latex_to_marked(BANK[m][i])} for i in range(5)]}
    result, _, changes = rt.apply_edits(BANK, edits, SLOTS)
    assert result[m] == SECTIONS[m]
    assert any(c.get("status") == "ignored reorder" for c in changes)


def test_revert_alternate_swaps_back_an_unused_default():
    m = "JOBAGENT_BULLETS"
    alt = SLOTS[m] + 1
    edits = {m: [{"id": f"{m}-{i}", "text": rt.latex_to_marked(BANK[m][i])} for i in (0, alt, 1, 2)]}
    result, sources, changes = rt.apply_edits(BANK, edits, SLOTS)
    assert rt.revert_alternate(result, sources, BANK, SLOTS, changes)
    assert result[m][1] == BANK[m][3] and sources[m][1] == f"{m}-3"
    assert not rt.revert_alternate(result, sources, BANK, SLOTS, changes)


def test_no_edits_with_bank_reproduces_template_exactly():
    result, _, _ = rt.apply_edits(BANK, {}, SLOTS)
    assert rt.render(TEMPLATE, result) == TEMPLATE


def test_project_order_renders_and_round_trips():
    names = [n for n, _ in rt.extract_projects(TEMPLATE)]
    assert names == ["JobAgent", "Uplink", "Pathwise"]
    assert rt.render_projects(TEMPLATE, names) == TEMPLATE
    moved = rt.render_projects(TEMPLATE, ["Pathwise", "Uplink", "JobAgent"])
    assert moved.index("{Pathwise") < moved.index("{Uplink") < moved.index("{JobAgent")
    assert rt.render_projects(moved, names) == TEMPLATE
    assert rt.render_projects(TEMPLATE, ["Uplink", "Uplink", "JobAgent"]) == TEMPLATE   # not a permutation


def test_coursework_reorders_only_as_a_permutation():
    c = rt.extract_coursework(TEMPLATE)
    assert "Computer Systems & Assembly Language" in c
    assert rt.render_coursework(TEMPLATE, c) == TEMPLATE
    out = rt.render_coursework(TEMPLATE, c[::-1])
    assert "Coursework:} Statistics, Calculus I--II" in out and "Systems \\& Assembly" in out
    assert rt.render_coursework(TEMPLATE, c[:-1]) == TEMPLATE                            # dropped one


def test_skills_round_trip_and_validation():
    sk = rt.extract_skills(TEMPLATE)
    inv = rt.skill_inventory(sk, ["TensorBoard"], EXTRAS)
    assert rt.render_skills(TEMPLATE, sk) == TEMPLATE
    assert rt.validate_skills(sk, inv, sk) is None
    bad = [dict(l) for l in sk]
    bad[0] = {"label": "Languages", "items": sk[0]["items"] + ["Rust"]}
    assert "unknown skill" in rt.validate_skills(bad, inv, sk)
    dup = [dict(l) for l in sk]
    dup[1] = {"label": dup[1]["label"], "items": dup[1]["items"] + ["Python"]}
    assert "duplicate" in rt.validate_skills(dup, inv, sk)
    assert "3 or 4" in rt.validate_skills(sk[:2], inv, sk)
    thin = [{"label": "A", "items": ["Python"]}, {"label": "B", "items": ["C"]}, {"label": "C", "items": ["Git"]}]
    assert "dropped too many" in rt.validate_skills(thin, inv, sk)
    systems = ["Linux", "Git", "Kubernetes", "SQLite", "PostgreSQL", "REST APIs", "pytest", "GitHub Actions (CI/CD)"]
    regrouped = [{"label": "Languages", "items": ["C", "C++", "Assembly", "Python", "Java", "JavaScript/TypeScript", "SQL", "HTML/CSS"]},
                 {"label": "Systems & Tools", "items": systems}, sk[1], sk[3]]
    assert rt.validate_skills(regrouped, inv, sk) is None
    out = rt.render_skills(TEMPLATE, regrouped, inv)
    assert "\\textbf{Languages:} C, C++, Assembly, Python" in out
    assert "\\textbf{Systems \\& Tools:} Linux, Git, Kubernetes" in out


def test_prompt_lists_alternates_projects_and_skill_inventory():
    sk = rt.extract_skills(TEMPLATE)
    inv = rt.skill_inventory(sk, [], EXTRAS)
    p = rt.build_prompt(BANK, {"role_title": "R", "company": "C"}, slots=SLOTS,
                        projects=["JobAgent", "Uplink", "Pathwise"],
                        coursework=rt.extract_coursework(TEMPLATE), skills=sk, inventory=inv)
    assert "// alternate" in p and "pick exactly 4" in p
    assert "project_order" in p and "coursework_order" in p and "Playwright" in p


def test_fit_skills_trims_the_tail_of_overlong_lines():
    long_line = {"label": "Systems, Tools & DevOps", "items": ["Linux", "Git", "Kubernetes", "GitHub Actions (CI/CD)",
                 "PostgreSQL", "SQLite", "REST APIs", "pytest", "Playwright", "TensorBoard", "Google Sheets API"]}
    ok = {"label": "Languages", "items": ["C", "C++"]}
    lines, dropped = rt.fit_skills([long_line, ok])
    assert lines[1] == ok and dropped and dropped[0] == "Google Sheets API"
    l0 = lines[0]
    assert len(l0["label"]) + 2 + len(", ".join(l0["items"])) <= rt.SKILL_LINE_MAX
    assert l0["items"][:3] == ["Linux", "Git", "Kubernetes"]
    assert rt.fit_skills(None) == (None, [])


def test_resume_and_cover_letter_use_school_email():
    # Haatim's application email is htali@ucsc.edu (2026-10-26)
    here = os.path.dirname(__file__)
    for name in ("ResumeTemplate.tex", "CoverLetterTemplate.tex", "experience_kb.yaml"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert "htali@ucsc.edu" in text and "haatimta@gmail.com" not in text, name
