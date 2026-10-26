"""
Resume tailoring that preserves the baseline resume's exact formatting.

ResumeTemplate.tex is Haatim's real resume. Tailoring never regenerates it; it only
applies small, validated edits to the bullets inside the %%X_START%% / %%X_END%% sections:

  * reorder bullets within a section (most relevant first)
  * reword a bullet toward the job's language, keeping every fact and number
  * move existing bold emphasis onto job-relevant terms (never add bold to an unbolded bullet)

Every edit is checked in code. An edit that drops a number, grows the bullet, adds or
removes a bullet, or breaks bold markup is rejected and the original bullet is kept.
After compiling, if the page has more text lines than the untouched baseline (or spills
onto a second page), the edits that grew the most are reverted until it fits the same
footprint as the original resume.
"""

import json
import logging
import re

SECTIONS = ["AIEA_BULLETS", "MONTESSORI_BULLETS", "UPLINK_BULLETS", "PATHWISE_BULLETS", "JOBAGENT_BULLETS"]
SECTION_LABELS = {
    "AIEA_BULLETS": "Undergraduate Research Assistant, Autonomous Vehicles (AIEA Lab)",
    "MONTESSORI_BULLETS": "Website Developer (Golden Harvest Montessori)",
    "UPLINK_BULLETS": "Project: Uplink, Multi-Agent Food Rescue Negotiation (hackathon, team of 3)",
    "PATHWISE_BULLETS": "Project: Pathwise, Autonomous Planning Agent (hackathon)",
    "JOBAGENT_BULLETS": "Project: JobAgent, Autonomous Internship Search & Tailoring Pipeline",
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


def apply_edits(sections, edits):
    """
    sections: {marker: [latex, ...]} from the template.
    edits: {marker: [{"id": "AIEA_BULLETS-0", "text": "..."}, ...]} from Claude, in new order.
    Returns (result, sources, changes):
      result  {marker: [latex, ...]} to render
      sources {marker: [id, ...]}    which original bullet sits at each position
      changes [..]                   what was edited / rejected, for the log
    """
    result, sources, changes = {}, {}, []
    for marker, originals in sections.items():
        marked = [latex_to_marked(b) for b in originals]
        ids = [f"{marker}-{i}" for i in range(len(originals))]
        proposed = edits.get(marker) if isinstance(edits, dict) else None
        proposed = [p for p in proposed if isinstance(p, dict)] if isinstance(proposed, list) else []

        # Order must be a permutation of the original ids, else keep the original order
        got = [p.get("id") for p in proposed]
        order = got if sorted(got) == sorted(ids) else ids
        if proposed and order is ids and got != ids:
            changes.append({"section": marker, "status": "ignored reorder", "reason": f"ids {got}"})
        texts = {p.get("id"): p.get("text") for p in proposed}

        out = []
        for bid in order:
            idx = ids.index(bid)
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


def count_reworded(sections, edits):
    """Number of bullets whose proposed text differs from the original (ignoring bold markers)."""
    n = 0
    for marker, originals in sections.items():
        proposed = edits.get(marker) if isinstance(edits, dict) else None
        if not isinstance(proposed, list):
            continue
        texts = {p.get("id"): p.get("text") for p in proposed if isinstance(p, dict)}
        for i, b in enumerate(originals):
            new = texts.get(f"{marker}-{i}")
            if isinstance(new, str) and plain(new).strip() != plain(latex_to_marked(b)).strip():
                n += 1
    return n


def build_retry_note(changed, sections):
    """Follow-up instruction when the edit pass reworded too few bullets."""
    lo, hi = rewrite_target(sum(len(b) for b in sections.values()))
    return (f"\n\nYour previous answer reworded only {changed} bullet(s), so the resume was not tailored "
            f"to this job. Reword {lo} to {hi} bullets this time, using the job's terminology "
            "wherever it truthfully fits. The same hard rules apply.")


def build_prompt(sections, analysis, custom_prompt="", job_description=""):
    blocks = []
    for marker, bullets in sections.items():
        lines = [f'  {{"id": "{marker}-{i}", "text": {json.dumps(latex_to_marked(b), ensure_ascii=False)}}}'
                 for i, b in enumerate(bullets)]
        blocks.append(f'"{marker}": [  // {SECTION_LABELS[marker]}\n' + ",\n".join(lines) + "\n]")
    total = sum(len(b) for b in sections.values())
    lo, hi = rewrite_target(total)
    custom = f"\nADDITIONAL INSTRUCTIONS FROM THE CANDIDATE:\n{custom_prompt}\n" if custom_prompt else ""
    jd = f"\nJob description (excerpt):\n{job_description[:3500]}\n" if job_description else ""
    return f"""You are tailoring a strong one-page resume so it reads as written for this specific job:
Role: {analysis.get("role_title")} at {analysis.get("company")}
Must have: {json.dumps(analysis.get("must_have", []))}
Nice to have: {json.dumps(analysis.get("nice_to_have", []))}
ATS keywords: {json.dumps(analysis.get("ats_keywords", []))}
{jd}{custom}
The candidate's resume is carefully formatted: its layout must stay identical (same sections,
same number of bullets, similar bullet lengths), but its WORDING must shift toward this job.
Your ONLY allowed changes, per section:
1. Reorder bullets so the most relevant to this job comes first.
2. Reword bullets to use the job's terminology wherever it TRUTHFULLY describes what the bullet
   already says, so a recruiter or ATS scanning for this job's keywords finds them. Reword
   {lo} to {hi} of the {total} bullets (this is required, not optional), prioritising the ones most
   relevant to the job; leave the rest exactly as they are. Good edits swap a generic word for the
   job's precise term, or bring forward the part of the bullet this job cares about most (e.g. for
   an infrastructure role, the GPU / Kubernetes orchestration; for an ML role, the model and
   training). Every bullet already has some wording a job-specific term can truthfully replace.
   Bad edits claim anything the bullet doesn't already say.
3. Keep bold as it is: a bullet with no **bold** must stay without bold; where a bullet has
   bold, you may move it onto the most job-relevant term or number already in the bullet.

Think about which of the candidate's real strengths this job values most, and make sure those
bullets come first in their section and read in the job's language.

Hard rules (edits that break these are automatically discarded):
- Keep every number. Never add a tool, metric, outcome or responsibility that isn't already in
  that bullet.
- Same length or shorter: never more than 5% longer than the original bullet. The bullets already
  fill their lines, so make room for the job's terms by swapping words rather than adding them, or
  by dropping a minor detail this job cares less about (never a number).
- Keep every bullet: same ids, no additions, no deletions, no merging.
- No em dashes unless the original bullet already has one. Keep **bold** markers balanced.
- Match the existing style: past-tense action verb first, no period at the end. Bullets are
  result-first (outcome, then "by ..." how): keep that shape, but you may reword both the
  outcome and the "by ..." part in the job's terms, or lead with a different true outcome.

Current bullets (text uses **bold** markers):
{{
{",".join(blocks)}
}}

Return JSON only, same shape, every id exactly once, in your chosen order:
{{"AIEA_BULLETS": [{{"id": "AIEA_BULLETS-2", "text": "..."}}, ...], "MONTESSORI_BULLETS": [...], "UPLINK_BULLETS": [...], "PATHWISE_BULLETS": [...], "JOBAGENT_BULLETS": [...]}}"""
