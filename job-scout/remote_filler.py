#!/usr/bin/env python3
"""
Remote filler: fills application forms in a browser that lives on the VPS, so they're waiting,
already filled, for Haatim to open from any device and submit. No laptop needed.

How it fits together (all on the VPS, see docs Part 6 "The remote filler"):

  Xvfb :99            a virtual screen (no monitor) the browser draws on
  x11vnc              shares that screen over VNC, on localhost only
  websockify/noVNC    turns VNC into a web page, reachable only over Tailscale
  remote_filler.py    this file: a real Chromium (Playwright) on that screen, plus a tiny
                      web server with one link per filled form

Every 30 seconds it takes each "ready" packet it hasn't filled yet, opens the form in a new
tab, fills it (same steps as form_fill.py: real keystrokes for dropdowns, text last, files,
then a read-back check), and posts one message in #applications with a link. Opening the
link brings that tab to the front and shows the screen in the browser of whichever device
opened it. Haatim does the CAPTCHA and clicks Submit; when a tab reaches a confirmation page
it's logged automatically (apply_prep.mark_submitted: ✅ in Discord, Google Sheet).

It never clicks Submit, never ticks agreements (those are listed in the Discord message for
Haatim to tick himself), and never fills questions the packet couldn't answer.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3

from dotenv import load_dotenv

import apply_prep
import form_fill

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.expanduser("~/.jobbot-browser")
BIND_HOST = os.getenv("REMOTE_FILLER_HOST", "127.0.0.1")   # the Tailscale IP in production
PORT = int(os.getenv("REMOTE_FILLER_PORT", "8765"))
VIEWER_PORT = int(os.getenv("REMOTE_VIEWER_PORT", "6080"))
MAX_OPEN = int(os.getenv("REMOTE_FILLER_MAX_TABS", "12"))
POLL_SECONDS = 30

CONFIRMATION = re.compile(r"thank(s| you) for (applying|your application)|application (was |has been )?"
                          r"(submitted|received)|we('ve| have) received your application", re.I)


# ── Planning (pure, no browser: tested in test_remote_filler.py) ──────────────

def _date_value(text):
    """'Any time after December 30, 2026' -> '12/31/2026'; 'March 2, 2027' -> '03/02/2027'."""
    from datetime import datetime, timedelta
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})", str(text))
    if not m:
        return None
    try:
        d = datetime.strptime(" ".join(m.groups()), "%B %d %Y")
    except ValueError:
        return None
    if re.search(r"\bafter\b", str(text), re.I):
        d += timedelta(days=1)
    return d.strftime("%m/%d/%Y")


def plan(packet, email=None):
    """Steps to fill one form. Returns
      {"url", "steps": [...], "text": {key: value}, "files": [(key, path)],
       "expect": {key: value}, "manual": [str], "agreements": [str]}
    step ops: "combo" (react-select: type then pick), "choice" (Ashby/Lever buttons, radios,
    checkboxes, <select>), "location", "date"."""
    email = email or form_fill.application_email()
    ats = packet.get("ats")
    pdir = packet.get("packet_dir", "")
    keys = {q["key"] for q in packet["questions"]}
    out = {"url": form_fill.form_url(packet), "steps": [], "text": {}, "files": [], "expect": {},
           "manual": [], "agreements": []}

    for q in packet["questions"]:
        key, ans, qtype, label = q["key"], q.get("answer"), q["type"], q["label"]
        if ats == "greenhouse" and key == "race" and "hispanic_ethnicity" not in keys:
            out["steps"].append({"op": "combo", "key": "hispanic_ethnicity", "type": "No", "pick": "No"})
        if key in ("email", "_systemfield_email") and email:
            ans = email
        if key == "first_name":
            ans = form_fill.FIRST_NAME
        if key == "last_name":
            ans = form_fill.LAST_NAME

        if qtype == "file":
            kind = "resume" if "resume" in key.lower() else "cover_letter" if "cover" in key.lower() else ""
            name = packet.get("files", {}).get(kind)
            if name:
                out["files"].append((key, os.path.join(pdir, name)))
            continue
        if ans in (None, "", []):
            if q.get("required"):
                out["manual"].append(label)
            continue
        if form_fill.is_agreement(q):
            out["agreements"].append(label)
            continue

        if qtype == "location":
            out["steps"].append({"op": "location", "key": key, "label": label})
            out["expect"][key] = form_fill.LOCATION_PICK if ats == "greenhouse" else "Fremont"
        elif qtype in ("text", "textarea"):
            date = _date_value(ans) if re.search(r"start date|date", label, re.I) else None
            if date and ats == "ashby":
                out["steps"].append({"op": "date", "key": key, "label": label, "value": date})
            else:
                out["text"][key] = str(ans)
                out["expect"][key] = str(ans)[:60]
        elif ats == "greenhouse" and qtype in ("select", "multiselect"):
            for v in ans if isinstance(ans, list) else [ans]:
                out["steps"].append({"op": "combo", "key": key, "type": str(v)[:40], "pick": str(v)})
            if key != "country":   # the country box only shows "+1"
                out["expect"][key] = ans
        elif qtype in ("select", "multiselect"):
            for v in ans if isinstance(ans, list) else [ans]:
                out["steps"].append({"op": "choice", "key": key, "label": label, "value": str(v)})
        else:
            out["manual"].append(f"{label} => {ans}")
    return out


# ── In-page helpers ───────────────────────────────────────────────────────────
# form_fill's helpers (find / open / pick / setText / verify), plus "choice" for the widgets
# Ashby and Lever use instead of dropdowns: buttons, radios, checkboxes and plain <select>.
_EXTRA = r"""
window.__f.field = function (key, label) {
  // The smallest ancestor of the field (or of its label) that holds the answer widgets
  const holds = n => n.querySelector('button, [role="radio"], [role="option"], input[type="radio"], input[type="checkbox"], select, input:not([type="hidden"])');
  const up = start => { for (let n = start; n && n !== document.body; n = n.parentElement) if (n !== start && holds(n)) return n; return null; };
  const el = this.find(key);
  if (el) return up(el) || el;
  const want = (label || '').toLowerCase().slice(0, 40);
  const lab = [...document.querySelectorAll('label, legend, .application-label, [class*="label"]')]
    .find(l => l.innerText && l.innerText.trim().toLowerCase().startsWith(want));
  return lab ? up(lab) : null;
};
window.__f.choice = function (key, label, value) {
  const v = value.trim().toLowerCase();
  const sel = document.querySelector(`select[name="${CSS.escape(key)}"]`);
  if (sel) {
    const o = [...sel.options].find(o => o.text.trim().toLowerCase() === v);
    if (!o) return 'NO OPTION ' + value;
    sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); return 'selected ' + o.text;
  }
  const inp = [...document.querySelectorAll(`input[name="${CSS.escape(key)}"]`)]
    .find(i => (i.value || '').trim().toLowerCase() === v);
  if (inp) { if (!inp.checked) inp.click(); return 'checked ' + inp.value; }
  const box = this.field(key, label);
  if (!box) return 'FIELD NOT FOUND ' + label;
  const hit = [...box.querySelectorAll('button, [role="radio"], [role="option"], label, span')]
    .find(b => b.innerText && b.innerText.trim().toLowerCase() === v);
  if (hit) { hit.click(); return 'clicked ' + hit.innerText.trim(); }
  return 'NO OPTION ' + value;
};
window.__f.focusIn = function (key, label) {
  const box = this.field(key, label);
  const el = this.find(key) || (box && box.querySelector('input'));
  if (!el) return 'missing';
  el.scrollIntoView({block: 'center'}); el.focus(); el.click(); this.cur = el; return 'ok';
};
"""


def setup_js():
    """The tested form_fill helpers, installed with no text (text is set last)."""
    js = form_fill._SETUP % "{}"
    # End on a plain value: Playwright calls the result if a script evaluates to a function
    return js.split("__f.text =")[0] + _EXTRA + "\ntrue;"


# ── The browser service ───────────────────────────────────────────────────────

class Filler:
    def __init__(self, context, public_host):
        self.ctx = context
        self.host = public_host
        self.open = {}          # job_url -> {"page", "slug", "company", "title"}
        self.closed = set()     # job_urls whose tab the user closed: don't refill until restart

    def link(self, slug):
        return f"http://{self.host}:{PORT}/open/{slug}"

    async def fill(self, job):
        pdir = os.path.join(apply_prep.PACKETS_DIR, job["packet"])
        with open(os.path.join(pdir, "packet.json")) as f:
            packet = json.load(f)
        packet["packet_dir"] = pdir
        p = plan(packet)
        page = await self.ctx.new_page()
        await page.goto(p["url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        await page.bring_to_front()
        await page.evaluate(setup_js())
        log = []
        for s in p["steps"]:
            if s["op"] == "combo":
                r = await page.evaluate("k => __f.open(k)", s["key"])
                if "missing" not in r:
                    await page.keyboard.type(s["type"], delay=15)
                    r = await page.evaluate("w => __f.pick(w)", s["pick"])
            elif s["op"] == "location":
                r = await page.evaluate("([k, l]) => __f.focusIn(k, l)", [s["key"], s["label"]])
                if r == "ok":
                    await page.keyboard.type(form_fill.LOCATION_SEARCH, delay=30)
                    r = await page.evaluate("w => __f.pick(w)", form_fill.LOCATION_PICK
                                            if packet["ats"] == "greenhouse" else "Fremont")
            elif s["op"] == "date":
                r = await page.evaluate("([k, l]) => __f.focusIn(k, l)", [s["key"], s["label"]])
                if r == "ok":
                    await page.keyboard.type(s["value"], delay=20)
                    await page.keyboard.press("Enter")
            else:
                r = await page.evaluate("([k, l, v]) => __f.choice(k, l, v)", [s["key"], s["label"], s["value"]])
            if isinstance(r, str) and (r.startswith("NO") or "NOT FOUND" in r):
                log.append(f"{s.get('label', s['key'])[:60]}: {r}")
        for key, path in p["files"]:
            loc = page.locator(f'input[type="file"][id="{key}"], input[type="file"][name="{key}"]')
            if await loc.count():
                await loc.first.set_input_files(path)
            else:
                log.append(f"file field {key} not found")
        await page.evaluate("t => __f.setText(t)", p["text"])
        await page.wait_for_timeout(1500)
        verify = json.loads(await page.evaluate("e => __f.verify(e)", p["expect"]))
        return page, p, log + verify["problems"]

    async def poll_ready(self, conn):
        for job in apply_prep.ready_packets(conn):
            url = job["job_url"]
            if url in self.open or url in self.closed or len(self.open) >= MAX_OPEN:
                continue
            slug = job["packet"]
            try:
                page, p, problems = await self.fill(job)
            except Exception as e:
                logging.exception(f"Fill failed: {url}")
                apply_prep.post_discord(f"⚠️ Couldn't fill {job['company']} {job['title']}: {str(e)[:200]}")
                self.closed.add(url)
                continue
            self.open[url] = {"page": page, "slug": slug, "company": job["company"], "title": job["title"]}
            needs = p["manual"] + [f"tick yourself: {a[:80]}" for a in p["agreements"]] + problems
            apply_prep.post_discord(
                f"🖱️ **Ready to submit:** {job['title']} at {job['company']}\n"
                f"Open: {self.link(slug)}\n"
                + ("❓ Check before submitting:\n" + "\n".join(f"• {n[:150]}" for n in needs[:8]) + "\n"
                   if needs else "All fields filled and checked. ")
                + "Do the CAPTCHA if one appears, then Submit.", ping=True)
            logging.info(f"Filled {url}: {len(needs)} item(s) need attention")

    async def poll_submitted(self):
        for url, info in list(self.open.items()):
            page = info["page"]
            if page.is_closed():
                self.open.pop(url)
                self.closed.add(url)
                continue
            try:
                text = (await page.inner_text("body"))[:3000]
            except Exception:
                continue
            if "confirmation" in page.url or CONFIRMATION.search(text):
                logging.info(f"Submitted: {url}")
                apply_prep.mark_submitted(url)
                self.open.pop(url)
                await page.close()

    async def loop(self):
        while True:
            conn = apply_prep.db_conn()
            try:
                await self.poll_submitted()
                await self.poll_ready(conn)
            except Exception:
                logging.exception("Filler loop error")
            finally:
                conn.close()
            await asyncio.sleep(POLL_SECONDS)


def index_html(filler):
    rows = "".join(f'<li><a href="/open/{i["slug"]}">{i["company"]}: {i["title"]}</a></li>'
                   for i in filler.open.values()) or "<li>Nothing waiting right now.</li>"
    return (f"<!doctype html><meta name=viewport content='width=device-width'><title>Job bot</title>"
            f"<h2>Applications ready to submit</h2><ul>{rows}</ul>"
            f"<p><a href='{viewer_url(filler.host)}'>Open the browser</a></p>")


def viewer_url(host):
    return f"http://{host}:{VIEWER_PORT}/vnc.html?autoconnect=true&resize=scale&reconnect=true"


async def serve(filler):
    from aiohttp import web

    async def index(_):
        return web.Response(text=index_html(filler), content_type="text/html")

    async def open_form(request):
        slug = request.match_info["slug"]
        for info in filler.open.values():
            if info["slug"] == slug and not info["page"].is_closed():
                await info["page"].bring_to_front()
                raise web.HTTPFound(viewer_url(filler.host))
        return web.Response(text="That form isn't open any more (submitted, closed, or the "
                                 "service restarted). See the list at /.", status=404)

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/open/{slug}", open_form)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, BIND_HOST, PORT).start()
    logging.info(f"Remote filler links on http://{filler.host}:{PORT}/")


async def main():
    from playwright.async_api import async_playwright
    logging.basicConfig(filename=os.getenv("REMOTE_FILLER_LOG", os.path.join(SCRIPT_DIR, "remote_filler.log")),
                        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, no_viewport=True,
            args=["--start-maximized", "--disable-background-timer-throttling",
                  "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                  "--no-first-run", "--no-default-browser-check"],
        )
        filler = Filler(ctx, os.getenv("REMOTE_FILLER_PUBLIC_HOST", BIND_HOST))
        await serve(filler)
        await filler.loop()


if __name__ == "__main__":
    asyncio.run(main())
