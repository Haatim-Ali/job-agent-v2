#!/usr/bin/env python3
"""
Turn an application packet into a ready-made fill plan for Claude in Chrome.

Filling a form one click at a time takes dozens of browser round trips. This builds the whole
fill up front so it runs as one `browser_batch` call:

  1. one javascript call that sets every text field and textarea at once
  2. three actions per dropdown (open it, type, click the exact matching option)
  3. a final javascript call that reads every field back and reports mismatches

File uploads aren't in the batch: the extension's upload tool needs an element ref from `find`.

  python form_fill.py <packet.json> <tabId>     # prints the plan as JSON

Runs on the Mac with no third-party packages (the answers file is read for the email override).
"""

import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ANSWERS_PATH = os.path.join(SCRIPT_DIR, "application_answers.yaml")

FIRST_NAME, LAST_NAME = "Haatim", "Ali"
LOCATION_SEARCH, LOCATION_PICK = "San Jose", "San Jose, California, United States"


def application_email(path=ANSWERS_PATH):
    """The email for application forms (application_answers.yaml), read without needing PyYAML."""
    try:
        with open(path) as f:
            m = re.search(r'^email:\s*"?([^"\s#]+)', f.read(), re.M)
            return m.group(1) if m else None
    except OSError:
        return None


def form_url(packet):
    """Greenhouse: always the embed form, because some companies (e.g. Waymo) redirect the
    hosted job page to their own careers site with the form buried in an iframe."""
    url = packet["form_url"]
    if packet.get("ats") == "greenhouse":
        m = re.search(r"greenhouse\.io/([\w-]+)/jobs/(\d+)", url)
        if m:
            return f"https://job-boards.greenhouse.io/embed/job_app?for={m.group(1)}&token={m.group(2)}"
    return url


# ── JavaScript snippets ───────────────────────────────────────────────────────
# The setup call installs small helpers on the page (window.__f), so every later step is a
# one-line call instead of a block of code. Text fields are set through React's native value
# setter plus an input event. Dropdowns need real keystrokes (computer "type"); __f.pick then
# waits for the matching option with a MutationObserver (no timer polling, which Chrome
# throttles in background tabs) and clicks the exact one.
# The tab must be visible on screen while filling: Chrome ignores typing in hidden windows.

_SETUP = """window.__f = {
 find: k => document.getElementById(k) || document.querySelector(`[name="${CSS.escape(k)}"]`),
 setText(vals) {
  const missing = [];
  for (const [k, v] of Object.entries(vals)) {
   const el = this.find(k);
   if (!el) { missing.push(k); continue; }
   const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
   Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
   for (const t of ['input', 'change', 'blur']) el.dispatchEvent(new Event(t, {bubbles: true}));
  }
  return {filled: Object.keys(vals).length - missing.length, missing};
 },
 open(k) {
  const el = this.find(k);
  if (!el) { document.activeElement && document.activeElement.blur(); this.cur = null; return k + ' missing (skip)'; }
  el.scrollIntoView({block: 'center'}); el.focus(); el.click(); this.cur = el;
  return document.hidden ? 'WARNING: tab hidden, typing will be ignored' : 'ok';
 },
 options() {
  return [...document.querySelectorAll('[role="option"]')].filter(o => !o.closest('[class*="iti"]'));
 },
 pick(want, ms = 6000) {
  const w = want.toLowerCase(), t = o => o.innerText.trim().toLowerCase();
  const get = () => { const os = this.options(); return os.find(o => t(o) === w) || os.find(o => t(o).startsWith(w)); };
  return new Promise(res => {
   const done = h => { h.click(); res('picked ' + h.innerText.trim()); };
   let h = get(); if (h) return done(h);
   const ob = new MutationObserver(() => { h = get(); if (h) { ob.disconnect(); done(h); } });
   ob.observe(document.body, {childList: true, subtree: true});
   setTimeout(() => { ob.disconnect(); res('NO OPTION ' + want); }, ms);
  });
 },
 shown(el) {
  if (el.getAttribute('role') !== 'combobox') return el.value;
  const box = el.closest('.select__control, [class*="-control"]');
  const chips = box ? [...box.querySelectorAll('[class*="single-value"], [class*="singleValue"], [class*="multi-value__label"]')] : [];
  return chips.map(c => c.innerText.trim()).join(' | ') || el.value;
 },
 verify(expect) {
  const norm = s => String(s).toLowerCase().replace(/[^a-z0-9@.]/g, '');
  const problems = [];
  for (const [k, v] of Object.entries(expect)) {
   const el = this.find(k), got = el ? this.shown(el) : '(field not found)';
   for (const want of [].concat(v)) if (!norm(got).includes(norm(want))) problems.push(`${k}: expected "${want}", got "${got}"`);
  }
  return JSON.stringify({checked: Object.keys(expect).length, problems,
                         files: document.body.innerText.match(/[\\w-]+\\.pdf/g) || []});
 },
};
__f.text = %s;
JSON.stringify({hidden: document.hidden})"""


def _js(tab_id, code):
    return {"name": "javascript_tool", "input": {"tabId": tab_id, "action": "javascript_exec", "text": code}}


def _combo(tab_id, key, typed, pick, wait=0):
    return [_js(tab_id, f"__f.open({json.dumps(key)})"),
            {"name": "computer", "input": {"tabId": tab_id, "action": "type", "text": typed}},
            _js(tab_id, f"await __f.pick({json.dumps(pick)})")]


# ── The plan ──────────────────────────────────────────────────────────────────

AGREEMENT_RE = re.compile(r"\b(acknowledg\w*|agree\w*|consent\w*|certif\w*|attest\w*|privacy polic\w*)\b", re.I)


def is_agreement(q):
    """Questions that accept terms or policies on the user's behalf: never in the automatic
    batch; the user approves them first."""
    return bool(AGREEMENT_RE.search(q.get("label", "")) or
                any(AGREEMENT_RE.search(o) for o in q.get("options", [])[:3]))


def build_plan(packet, tab_id, email=None):
    """Returns {"url", "files", "actions", "verify", "manual", "agreements"}:
    actions: browser_batch items that fill the form (navigate first, verify last)
    files:   [{"kind", "path"}] to upload with file_upload after a `find`
    manual:  questions the plan can't fill (no answer, or an unsupported widget)
    agreements: [{"label", "answer", "actions"}] to run only after the user says yes"""
    email = email or application_email()
    ats = packet.get("ats")
    text, combos, expect, manual, files, agreements = {}, [], {}, [], [], []
    packet_dir = packet.get("packet_dir", "")

    keys = {q["key"] for q in packet["questions"]}
    for q in packet["questions"]:
        key, ans, qtype = q["key"], q.get("answer"), q["type"]
        # Greenhouse shows the race question only after "Are you Hispanic/Latino?" is answered,
        # and the API doesn't list that question: answer it first (not Hispanic/Latino).
        if ats == "greenhouse" and key == "race" and "hispanic_ethnicity" not in keys:
            combos.append(_combo(tab_id, "hispanic_ethnicity", "No", "No"))
        if key in ("email", "_systemfield_email") and email:
            ans = email
        if key == "first_name":
            ans = FIRST_NAME
        if key == "last_name":
            ans = LAST_NAME

        if qtype == "file":
            name = packet.get("files", {}).get("resume" if "resume" in key.lower() else
                                               "cover_letter" if "cover" in key.lower() else "")
            if name:
                files.append({"kind": key, "path": os.path.join(packet_dir, name)})
            continue
        if ans in (None, "", []):
            if q.get("required"):
                manual.append(q["label"])
            continue
        if is_agreement(q):
            agreements.append({"label": q["label"], "answer": ans,
                               "actions": _combo(tab_id, key, str(ans)[:40], str(ans))
                               if ats == "greenhouse" and q["type"] == "select" else []})
            continue
        if ats != "greenhouse":
            # Lever and Ashby widgets differ (radio groups, yes/no buttons): plain text fields
            # are planned, everything else is filled by hand.
            if qtype in ("text", "textarea"):
                text[key] = str(ans)
                expect[key] = str(ans)
            else:
                manual.append(f"{q['label']} => {ans}")
            continue

        if qtype in ("text", "textarea"):
            text[key] = str(ans)
            expect[key] = str(ans)
        elif qtype == "location":
            combos.append(_combo(tab_id, key, LOCATION_SEARCH, LOCATION_PICK))
            expect[key] = LOCATION_PICK
        elif qtype == "select":
            pick = str(ans)
            combos.append(_combo(tab_id, key, pick[:40], pick))
            # Country is a phone-code picker whose box shows only "+1"
            if key != "country":
                expect[key] = pick
        elif qtype == "multiselect":
            for v in ans if isinstance(ans, list) else [ans]:
                combos.append(_combo(tab_id, key, str(v)[:40], str(v)))
            expect[key] = [str(v) for v in (ans if isinstance(ans, list) else [ans])]
        else:
            manual.append(f"{q['label']} => {ans}")

    url = form_url(packet)
    actions = [{"name": "navigate", "input": {"tabId": tab_id, "url": url}},
               _js(tab_id, _SETUP % json.dumps(text, ensure_ascii=False)),
               # let the form finish loading before the first dropdown
               {"name": "computer", "input": {"tabId": tab_id, "action": "wait", "duration": 1}}]
    for steps in combos:
        actions.extend(steps)
    # Text goes in last: Greenhouse resets name/email fields while the form finishes loading
    actions.append(_js(tab_id, "JSON.stringify(__f.setText(__f.text))"))
    # Essays are checked by their opening words only, to keep the report short
    expect = {k: (v[:60] if isinstance(v, str) else v) for k, v in expect.items()}
    verify = _js(tab_id, f"__f.verify({json.dumps(expect, ensure_ascii=False)})")
    return {"url": url, "files": files, "actions": actions, "verify": verify, "manual": manual,
            "agreements": agreements}


def main():
    packet_path, tab_id = sys.argv[1], int(sys.argv[2])
    with open(packet_path) as f:
        packet = json.load(f)
    packet["packet_dir"] = os.path.dirname(os.path.abspath(packet_path))
    print(json.dumps(build_plan(packet, tab_id), ensure_ascii=False))


if __name__ == "__main__":
    main()
