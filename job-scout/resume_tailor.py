"""
Resume tailoring that preserves the baseline resume's exact formatting.

ResumeTemplate.tex is Haatim's real resume. Tailoring never regenerates it; between the
%%X_START%% / %%X_END%% markers it may only:

  * pick each section's bullets from the template's defaults plus the approved alternates in
    bullet_bank.yaml (same number of bullets per section), most relevant first
  * reword a picked bullet toward the job's language, keeping every number and claiming nothing new
  * reorder the projects and the coursework list
  * regroup and reorder Technical Skills, using only skills Haatim really has

Every change is checked in code (numbers, length, bold, skills inventory, permutations) and every
reworded bullet is fact-checked against the bullet it came from. After compiling, if the page has
more text lines than the untouched baseline, rewordings are reverted (largest growth first), then
alternates swapped back for defaults, then the skills restored, until it fits.
"""

import json
import logging
import os
import re

import yaml

SECTIONS = ["AIEA_BULLETS", "MONTESSORI_BULLETS", "UPLINK_BULLETS", "PATHWISE_BULLETS", "JOBAGENT_BULLETS"]
SECTION_LABELS = {
    "AIEA_BULLETS": "Undergraduate Research Assistant, Autonomous Vehicles (AIEA Lab)",
    "MONTESSORI_BULLETS": "Website Developer (Golden Harvest Montessori)",
    "UPLINK_BULLETS": "Project: Uplink, Multi-Agent Food Rescue Negotiation (hackathon, team of 3)",
    "PATHWISE_BULLETS": "Project: Pathwise, Autonomous Financial Planning Agent (hackathon)",
    "JOBAGENT_BULLETS": "Project: JobAgent, Autonomous Internship Search & Resume Tailoring Pipeline",
}

MAX_GROWTH = 1.05      # a reworded bullet may be at most 5% longer than the original...
GROWTH_SLACK = 6       # ...plus a few characters
MIN_SHRINK = 0.70      # and must keep at least 70% of the original length


# ── LaTeX <-> marked text ─────────────────────────────────────────────────────

def _section_body(tex, marker):
    m = re.search(rf"% %%{marker}_START%%\n(.*?)% %%{marker}_END%%", tex, re.DOTALL)
    return m.group(1) if m else None


def _resitem_args(body):
    """Return the brace-balanced argument of every \\resitem{...} in body."""
    items, i = [], 0
    while True:
        start = body.find("\\resitem{", i)
        if start < 0:
            return items
        j, depth = start + len("\\resitem{"), 1
        while depth:
            c = body[j]
            if c == "\\":          # skip escaped char, e.g. \{ or \}
                j += 2
                continue
            depth += {"{": 1, "}": -1}.get(c, 0)
            j += 1
        items.append(body[start + len("\\resitem{"): j - 1])
        i = j


def extract_sections(tex):
    """{marker: [bullet LaTeX, ...]} for every tailorable section in the template."""
    out = {}
    for marker in SECTIONS:
        body = _section_body(tex, marker)
        if body is None:
            raise ValueError(f"Resume template is missing section {marker}")
        out[marker] = _resitem_args(body)
    return out


_UNESCAPE = [("\\textbf{", "**"), ("\\&", "&"), ("\\%", "%"), ("\\$", "$"), ("\\#", "#"),
             ("\\_", "_"), ("---", "\u2014"), ("--", "\u2013"), ("\\(\\rightarrow\\)", "\u2192"),
             ("\\textasciitilde{}", "~")]


def latex_to_marked(s):
    """LaTeX bullet -> plain text with **bold** markers (what Claude sees and edits)."""
    s = re.sub(r"\\textbf\{([^{}]*)\}", r"**\1**", s)
    for a, b in _UNESCAPE[1:]:
        s = s.replace(a, b)
    return s


def _escape(text):
    text = text.replace("\\", r"\textbackslash{}")
    for char, esc in [("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
                      ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"),
                      ("^", r"\textasciicircum{}")]:
        text = text.replace(char, esc)
    return (text.replace("\u2014", "---").replace("\u2013", "--")
                .replace("\u2192", r"\(\rightarrow\)")
                .replace("\u2018", "`").replace("\u2019", "'")
                .replace("\u201c", "``").replace("\u201d", "''"))


def marked_to_latex(s):
    """Plain text with **bold** markers -> LaTeX, escaping everything else."""
    parts = s.split("**")
    return "".join(_escape(p) if i % 2 == 0 else f"\\textbf{{{_escape(p)}}}"
                   for i, p in enumerate(parts))


def plain(s):
    return s.replace("**", "")


# ── Validation ────────────────────────────────────────────────────────────────

_NUM = re.compile(r"\d+(?:\.\d+)?\+?%?")


def validate_edit(original, edited):
    """Return None if the edit is acceptable, else the reason it was rejected."""
    if not isinstance(edited, str) or not edited.strip():
        return "empty"
    if edited.count("**") % 2:
        return "unbalanced bold markers"
    o, e = plain(original), plain(edited)
    if len(e) > len(o) * MAX_GROWTH + GROWTH_SLACK:
        return f"too long ({len(e)} vs {len(o)} chars)"
    if len(e) < len(o) * MIN_SHRINK:
        return f"dropped too much ({len(e)} vs {len(o)} chars)"
    missing = [n for n in _NUM.findall(o) if n not in e]
    if missing:
        return f"lost numbers {missing}"
    new_nums = [n for n in _NUM.findall(e) if n not in o]
    if new_nums:
        return f"introduced numbers {new_nums}"
    bold_o, bold_e = original.count("**") // 2, edited.count("**") // 2
    if bold_o and not bold_e:
        return "removed all bold emphasis"
    if bold_e > bold_o + 1 or (bold_e and not bold_o):
        return "too much bold"
    if "\u2014" in e and "\u2014" not in o:
        return "added an em dash"
    return None


def apply_edits(sections, edits, slots=None):
    """
    sections: {marker: [latex, ...]}: the bullet bank (template defaults first, then alternates).
    edits: {marker: [{"id": "AIEA_BULLETS-0", "text": "..."}, ...]} from Claude, in new order.
    slots: {marker: n} bullets shown per section (default: all of them, i.e. a pure reorder).
    Returns (result, sources, changes):
      result  {marker: [latex, ...]} to render
      sources {marker: [id, ...]}    which bank bullet sits at each position
      changes [..]                   what was edited / rejected, for the log
    """
    slots = slots or {}
    result, sources, changes = {}, {}, []
    for marker, originals in sections.items():
        n = slots.get(marker, len(originals))
        marked = [latex_to_marked(b) for b in originals]
        ids = [f"{marker}-{i}" for i in range(len(originals))]
        proposed = edits.get(marker) if isinstance(edits, dict) else None
        proposed = [p for p in proposed if isinstance(p, dict)] if isinstance(proposed, list) else []

        # A valid pick is n distinct bank ids; otherwise keep the defaults in their original order
        got = [p.get("id") for p in proposed]
        default = ids[:n]
        valid = len(got) == n and len(set(got)) == n and all(g in ids for g in got)
        order = got if valid else default
        if proposed and not valid and got != default:
            changes.append({"section": marker, "status": "ignored reorder", "reason": f"ids {got}"})
        texts = {p.get("id"): p.get("text") for p in proposed}

        out = []
        for bid in order:
            idx = ids.index(bid)
            if idx >= n:
                changes.append({"id": bid, "status": "alternate"})
            new = texts.get(bid)
            if isinstance(new, str) and "**" not in marked[idx]:
                new = plain(new)   # bullets without bold stay without bold; keep the rewording
            if new is None or new == marked[idx]:
                out.append(originals[idx])
                continue
            reason = validate_edit(marked[idx], new)
            if reason:
                out.append(originals[idx])
                changes.append({"id": bid, "status": "rejected", "reason": reason})
            else:
                out.append(marked_to_latex(new))
                changes.append({"id": bid, "status": "edited",
                                "growth": len(plain(new)) - len(plain(marked[idx]))})
        result[marker], sources[marker] = out, list(order)
    return result, sources, changes


def revert_alternate(result, sources, sections, slots, changes):
    """Swap the longest alternate bullet back for an unused default bullet of its section.
    Returns False when no alternates are left."""
    best = None
    for marker, order in sources.items():
        n = slots.get(marker, len(sections[marker]))
        for pos, bid in enumerate(order):
            idx = int(bid.rsplit("-", 1)[1])
            if idx >= n:
                size = len(plain(latex_to_marked(result[marker][pos])))
                if best is None or size > best[0]:
                    best = (size, marker, pos, bid)
    if best is None:
        return False
    _, marker, pos, bid = best
    n = slots.get(marker, len(sections[marker]))
    used = {int(b.rsplit("-", 1)[1]) for b in sources[marker]}
    spare = next(i for i in range(n) if i not in used)
    result[marker][pos] = sections[marker][spare]
    sources[marker][pos] = f"{marker}-{spare}"
    for c in changes:
        if c.get("id") == bid and c.get("status") in ("alternate", "edited"):
            c["status"] = "alternate reverted (layout)" if c["status"] == "alternate" else "reverted (layout)"
    return True


# ── Bullet bank, projects, coursework, skills ─────────────────────────────────

BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bullet_bank.yaml")


def load_bank(sections, path=BANK_PATH):
    """(bank, slots, skill_extras): template defaults followed by the approved alternates."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    bank, slots = {}, {}
    for marker, defaults in sections.items():
        extra = [marked_to_latex(t) for t in data.get(marker) or [] if isinstance(t, str)]
        extra = [e for e in extra if e not in defaults]
        bank[marker], slots[marker] = list(defaults) + extra, len(defaults)
    return bank, slots, [x for x in data.get("skill_extras") or [] if isinstance(x, str)]


def _block(tex, marker):
    m = re.search(rf"% %%{marker}_START%%\n(.*?)% %%{marker}_END%%\n", tex, re.DOTALL)
    if not m:
        raise ValueError(f"Resume template is missing {marker} markers")
    return m


def _replace_block(tex, marker, body):
    m = _block(tex, marker)
    return tex[:m.start(1)] + body + tex[m.end(1):]


def extract_projects(tex):
    """[(name, block_text)] in template order; name is the first word of the heading."""
    body = _block(tex, "PROJECTS").group(1)
    chunks = [c for c in re.split(r"(?=\\ressubheading\{)", body) if c.strip()]
    return [(re.match(r"\\ressubheading\{(\w+)", c).group(1), c) for c in chunks]


def render_projects(tex, order):
    projects = dict(extract_projects(tex))
    if sorted(order or []) != sorted(projects):
        return tex
    parts = [projects[n].rstrip("\n") + "\n" for n in order]
    return _replace_block(tex, "PROJECTS", "\n".join(parts))


def _coursework_latex(tex):
    line = _block(tex, "COURSEWORK").group(1)
    m = re.search(r"\\textbf\{Relevant Coursework:\}\s*(.*)\}\s*$", line.strip())
    return line, ([c.strip() for c in m.group(1).split(",")] if m else [])


def extract_coursework(tex):
    """Course names as plain text, in template order."""
    return [latex_to_marked(c) for c in _coursework_latex(tex)[1]]


def render_coursework(tex, order):
    line, courses = _coursework_latex(tex)
    names = [latex_to_marked(c) for c in courses]
    if sorted(order or []) != sorted(names) or list(order) == names:
        return tex
    new = ", ".join(courses[names.index(c)] for c in order)
    return _replace_block(tex, "COURSEWORK", line.replace(", ".join(courses), new))


def extract_skills(tex):
    """[{"label": ..., "items": [...]}] from the Technical Skills block (plain text)."""
    out = []
    for arg in _resitem_args(_block(tex, "SKILLS").group(1)):
        m = re.match(r"\\textbf\{(.*?):\}\s*(.*)$", arg, re.DOTALL)
        if m:
            out.append({"label": latex_to_marked(m.group(1)),
                        "items": [latex_to_marked(x.strip()) for x in m.group(2).split(", ")]})
    return out


SKILL_LINE_MAX = 108    # characters of "Label: a, b, c" that fit on one line
SKILL_LABEL_MAX = 26


def skill_inventory(template_skills, kb_skills, extras):
    """Every skill the tailor may list (case-insensitive lookup -> canonical spelling)."""
    inv = {}
    for s in [i for line in template_skills for i in line["items"]] + list(kb_skills) + list(extras):
        if isinstance(s, str) and s.strip():
            inv.setdefault(s.strip().lower(), s.strip())
    return inv


def validate_skills(lines, inventory, template_skills):
    """None if the proposed skills block is acceptable, else the reason."""
    if not isinstance(lines, list) or not 3 <= len(lines) <= 4:
        return "need 3 or 4 skill lines"
    seen, total = set(), 0
    for line in lines:
        label, items = (line or {}).get("label"), (line or {}).get("items")
        if not isinstance(label, str) or not label.strip() or len(label) > SKILL_LABEL_MAX:
            return f"bad label {label!r}"
        if not isinstance(items, list) or not items:
            return f"no items for {label}"
        for it in items:
            key = it.strip().lower() if isinstance(it, str) else None
            if key not in inventory:
                return f"unknown skill {it!r}"
            if key in seen:
                return f"duplicate skill {it!r}"
            seen.add(key)
        total += len(items)
        if len(label) + 2 + len(", ".join(items)) > SKILL_LINE_MAX:
            return f"line too long: {label}"
    if total < sum(len(l["items"]) for l in template_skills) - 4:
        return "dropped too many skills"
    return None


def fit_skills(lines):
    """Trim the last (least relevant) items off any skill line too long to print on one line.
    Returns (lines, dropped_items). Leaves anything malformed for validate_skills to reject."""
    if not isinstance(lines, list):
        return lines, []
    out, dropped = [], []
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("items"), list) or not isinstance(line.get("label"), str):
            out.append(line)
            continue
        items = [i for i in line["items"]]
        while len(items) > 1 and len(line["label"]) + 2 + len(", ".join(map(str, items))) > SKILL_LINE_MAX:
            dropped.append(items.pop())
        out.append({"label": line["label"], "items": items})
    return out, dropped


def render_skills(tex, lines, inventory=None):
    def canon(x):
        return inventory.get(x.strip().lower(), x.strip()) if inventory else x.strip()
    body = "".join(f"    \\resitem{{\\textbf{{{_escape(l['label'].strip())}:}} "
                   f"{', '.join(_escape(canon(i)) for i in l['items'])}}}\n" for l in lines)
    return _replace_block(tex, "SKILLS", body)


def render(tex, sections):
    for marker, bullets in sections.items():
        body = "".join(f"    \\resitem{{{b}}}\n" for b in bullets)
        tex = re.sub(rf"(% %%{marker}_START%%\n).*?(% %%{marker}_END%%)",
                     lambda m: m.group(1) + body + m.group(2), tex, flags=re.DOTALL)
    return tex


def revert_one(result, sources, originals, changes):
    """Put back the original text of the applied edit that grew the most.
    Keeps the new bullet order. Returns False when no edits are left to revert."""
    edited = [c for c in changes if c.get("status") == "edited"]
    if not edited:
        return False
    worst = max(edited, key=lambda c: c["growth"])
    marker, idx = worst["id"].rsplit("-", 1)
    pos = sources[marker].index(worst["id"])
    result[marker][pos] = originals[marker][int(idx)]
    worst["status"] = "reverted (layout)"
    return True


# ── Truthfulness check ────────────────────────────────────────────────────────

def changed_pairs(sections, edits):
    """[(id, original_marked, edited_marked)] for every bullet the model actually changed."""
    pairs = []
    for marker, originals in sections.items():
        for p in (edits.get(marker) or []) if isinstance(edits, dict) else []:
            if not isinstance(p, dict) or not isinstance(p.get("text"), str):
                continue
            try:
                m, idx = p["id"].rsplit("-", 1)
                orig = latex_to_marked(originals[int(idx)])
            except (KeyError, ValueError, IndexError, AttributeError):
                continue
            if m == marker and p["text"] != orig:
                pairs.append((p["id"], orig, p["text"]))
    return pairs


def build_verify_prompt(pairs):
    rows = "\n\n".join(f'ID: {i}\nORIGINAL: {plain(o)}\nEDITED:   {plain(e)}' for i, o, e in pairs)
    return f"""You are a strict fact-checker for a resume. Each EDITED bullet was reworded from the ORIGINAL
to match a job posting. The candidate's original bullet is the only source of truth.

Reject an edit if it claims or implies ANYTHING the original does not support, including:
- relabeling the work as something it wasn't (e.g. calling Discord alerts "telemetry", a website "a platform
  serving millions", a class project "production"), or adding a domain the work wasn't in
- a new tool, language, technique, scale, outcome, responsibility or level of ownership
- a stronger claim ("led" for "helped", "designed" for "implemented", "production" for "demo")
Accept edits that only reorder clauses, change emphasis, swap in an accurate synonym, or trim words.

{rows}

Return JSON only: {{"rejected": [{{"id": "...", "reason": "short reason"}}]}}  (empty list if all are faithful)"""


def drop_rejected(sections, edits, rejected_ids):
    """Replace the text of rejected edits with the original bullet (keeping the new order)."""
    for marker, proposed in (edits.items() if isinstance(edits, dict) else []):
        for p in proposed or []:
            if isinstance(p, dict) and p.get("id") in rejected_ids:
                m, idx = p["id"].rsplit("-", 1)
                p["text"] = latex_to_marked(sections[m][int(idx)])
    return edits


# ── Claude prompt ─────────────────────────────────────────────────────────────

def rewrite_target(total):
    """How many bullets the edit pass should reword: about 40-55% of them."""
    return max(1, round(total * 0.4)), max(1, round(total * 0.55))


def count_reworded(sections, edits, slots=None):
    """Bullets that differ from the default resume: reworded text, or an alternate swapped in."""
    slots = slots or {}
    n = 0
    for marker, originals in sections.items():
        proposed = edits.get(marker) if isinstance(edits, dict) else None
        if not isinstance(proposed, list):
            continue
        k = slots.get(marker, len(originals))
        for p in proposed:
            if not isinstance(p, dict):
                continue
            try:
                idx = int(str(p.get("id")).rsplit("-", 1)[1])
                orig = originals[idx]
            except (ValueError, IndexError):
                continue
            new = p.get("text")
            if idx >= k or (isinstance(new, str) and plain(new).strip() != plain(latex_to_marked(orig)).strip()):
                n += 1
    return n


def build_retry_note(changed, sections, slots=None):
    """Follow-up instruction when the edit pass changed too few bullets."""
    slots = slots or {}
    lo, hi = rewrite_target(sum(slots.get(m, len(b)) for m, b in sections.items()))
    return (f"\n\nYour previous answer changed only {changed} bullet(s), so the resume was not tailored "
            f"to this job. Change {lo} to {hi} bullets this time (reword them in the job's terminology "
            "wherever it truthfully fits, or swap in a more relevant alternate). The same hard rules apply.")


def build_prompt(sections, analysis, custom_prompt="", job_description="", slots=None,
                 projects=None, coursework=None, skills=None, inventory=None):
    slots = slots or {}
    blocks = []
    for marker, bullets in sections.items():
        n = slots.get(marker, len(bullets))
        lines = [f'  {{"id": "{marker}-{i}", "text": {json.dumps(latex_to_marked(b), ensure_ascii=False)}}}'
                 + ("  // default" if i < n else "  // alternate")
                 for i, b in enumerate(bullets)]
        blocks.append(f'"{marker}": [  // {SECTION_LABELS[marker]}: pick exactly {n}\n' + ",\n".join(lines) + "\n]")
    total = sum(slots.get(m, len(b)) for m, b in sections.items())
    lo, hi = rewrite_target(total)
    custom = f"\nADDITIONAL INSTRUCTIONS FROM THE CANDIDATE:\n{custom_prompt}\n" if custom_prompt else ""
    jd = f"\nJob description (excerpt):\n{job_description[:3500]}\n" if job_description else ""
    extra = ""
    if projects:
        extra += f"\nproject_order: the projects {json.dumps(projects)} in the order that best fits this job.\n"
    if coursework:
        extra += f"coursework_order: these courses, all of them, most relevant to this job first: {json.dumps(coursework)}\n"
    if skills and inventory:
        extra += (f"skills: 3 or 4 Technical Skills lines, each {{\"label\": ..., \"items\": [...]}}. Current lines:\n"
                  f"{json.dumps(skills, ensure_ascii=False)}\n"
                  "Regroup and reorder them for this job: put the job's must-have skills first, and you may rename the\n"
                  "labels (e.g. \"AI & Agents\", \"Systems & Tools\", \"Data & DevOps\"; at most 26 characters). Use ONLY skills\n"
                  f"from this list, spelled exactly as written, each at most once: {json.dumps(sorted(inventory.values()))}\n"
                  f"Keep nearly all current skills. Each line must fit on one printed line ({SKILL_LINE_MAX} characters including the label);\n"
                  "put the most job-relevant items first in each line, since overflow is cut from the end. Every item must\n"
                  "genuinely belong under its line's label (web frameworks are not \"AI\", databases are not \"Languages\").\n")
    return f"""You are tailoring a strong one-page resume so it reads as written for this specific job:
Role: {analysis.get("role_title")} at {analysis.get("company")}
Must have: {json.dumps(analysis.get("must_have", []))}
Nice to have: {json.dumps(analysis.get("nice_to_have", []))}
ATS keywords: {json.dumps(analysis.get("ats_keywords", []))}
{jd}{custom}
The resume's layout must stay identical (same sections, same number of bullets per section, similar
bullet lengths), but its CONTENT and WORDING must shift toward this job. Per section you may:
1. Pick which bullets to show: exactly as many as the section's default bullets, chosen from its
   defaults and its approved alternates (every one is true). Prefer whichever bullets best match
   this job's must-haves, and put the most relevant first.
2. Reword picked bullets to use the job's terminology wherever it TRUTHFULLY describes what the
   bullet already says, so a recruiter or ATS scanning for this job's keywords finds them.
   In total, change {lo} to {hi} of the {total} shown bullets (reworded or swapped-in alternates;
   this is required, not optional), prioritising the ones most relevant to the job. Good edits swap
   a generic word for the job's precise term, or bring forward the part of the bullet this job cares
   about most (e.g. for an infrastructure role, the GPU / Kubernetes orchestration; for an ML role,
   the model and training; for a systems role, Linux, processes, C/C++). Bad edits claim anything
   the bullet doesn't already say.
3. Keep bold as it is: a bullet with no **bold** must stay without bold.

Think about which of the candidate's real strengths this job values most, and make sure those
bullets come first in their section and read in the job's language.

Hard rules (edits that break these are automatically discarded):
- Keep every number in a bullet. Never add a tool, metric, outcome or responsibility that isn't
  already in that bullet.
- Same length or shorter: never more than 5% longer than the bullet you picked. The bullets already
  fill their lines, so make room for the job's terms by swapping words rather than adding them, or
  by dropping a minor detail this job cares less about (never a number).
- Use only the given ids, each at most once per section; no new bullets, no merging.
- No em dashes unless the original bullet already has one. Keep **bold** markers balanced.
- Match the existing style: past-tense action verb first, no period at the end. Bullets are
  result-first (outcome, then "by ..." how): keep that shape, but you may reword both the
  outcome and the "by ..." part in the job's terms, or lead with a different true outcome.
{extra}
Bullets (text uses **bold** markers):
{{
{",".join(blocks)}
}}

Return JSON only: {{"bullets": {{"AIEA_BULLETS": [{{"id": "AIEA_BULLETS-2", "text": "..."}}, ...], "MONTESSORI_BULLETS": [...], "UPLINK_BULLETS": [...], "PATHWISE_BULLETS": [...], "JOBAGENT_BULLETS": [...]}}, "project_order": [...], "coursework_order": [...], "skills": [...]}}"""
