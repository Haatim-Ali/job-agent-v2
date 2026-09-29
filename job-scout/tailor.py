#!/usr/bin/env python3
"""
Job Scout Tailor - generates tailored resume + cover letter as compiled PDFs.

Usage:
  python3 tailor.py <job_url>                    # saves PDFs to OUTPUT_DIR (or RESUME_SYNC_PATH)
  python3 tailor.py <job_url> <discord_channel>  # sends PDFs as Discord file attachments

Requires: pdflatex  (sudo apt-get install texlive-latex-base texlive-fonts-recommended texlive-latex-extra)
          pypdf     (pip install pypdf)
"""

import os
import re
import sys
import json
import yaml
import shutil
import asyncio
import logging
import tempfile
import subprocess
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic
import discord

import resume_tailor

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BOT_TOKEN         = os.getenv("DISCORD_BOT_TOKEN")
SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
KB_PATH           = os.path.join(SCRIPT_DIR, "experience_kb.yaml")
RESUME_TEMPLATE   = os.path.join(SCRIPT_DIR, "ResumeTemplate.tex")
COVER_TEMPLATE    = os.path.join(SCRIPT_DIR, "CoverLetterTemplate.tex")
OUTPUT_DIR        = os.getenv("OUTPUT_DIR", os.path.join(SCRIPT_DIR, "output"))
RESUME_SYNC_PATH  = os.getenv("RESUME_SYNC_PATH", "")   # optional local sync folder, set in .env

logging.basicConfig(
    filename="/var/log/jobscout_tailor.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


# ── LaTeX helpers ──────────────────────────────────────────────────────────────

def escape_latex(text):
    """Escape plain text for safe insertion into LaTeX source."""
    # Unicode smart quotes / dashes first (before char-level escapes)
    text = text.replace('\u2014', '---')
    text = text.replace('\u2013', '--')
    text = text.replace('\u2018', '`')
    text = text.replace('\u2019', "'")
    text = text.replace('\u201C', '``')
    text = text.replace('\u201D', "''")
    # Backslash must come before other replacements
    text = text.replace('\\', r'\textbackslash{}')
    for char, esc in [
        ('&',  r'\&'),
        ('%',  r'\%'),
        ('$',  r'\$'),
        ('#',  r'\#'),
        ('_',  r'\_'),
        ('{',  r'\{'),
        ('}',  r'\}'),
        ('~',  r'\textasciitilde{}'),
        ('^',  r'\textasciicircum{}'),
    ]:
        text = text.replace(char, esc)
    return text


def bullets_to_latex(bullets):
    return '\n'.join(f'    \\item {escape_latex(b)}' for b in bullets)


def replace_section(tex, marker, new_content):
    """Replace everything between % %%MARKER_START%% and % %%MARKER_END%%."""
    pattern = rf'% %%{re.escape(marker)}_START%%.*?% %%{re.escape(marker)}_END%%'
    replacement = f'% %%{marker}_START%%\n{new_content}\n% %%{marker}_END%%'
    result = re.sub(pattern, lambda m: replacement, tex, flags=re.DOTALL)
    if result == tex:
        logging.warning(f"Marker {marker} not found in template -- skipping replacement")
    return result


# ── Template builders ──────────────────────────────────────────────────────────

def build_cover_tex(data):
    with open(COVER_TEMPLATE, 'r', encoding='utf-8') as f:
        tex = f.read()

    cl = data.get('cover_letter', {})
    tex = tex.replace('%%DATE%%',        cl.get('date', datetime.now().strftime('%B %d, %Y')))
    tex = tex.replace('%%PARAGRAPH_1%%', escape_latex(cl.get('paragraph_1', '')))
    tex = tex.replace('%%PARAGRAPH_2%%', escape_latex(cl.get('paragraph_2', '')))
    tex = tex.replace('%%PARAGRAPH_3%%', escape_latex(cl.get('paragraph_3', '')))
    return tex


# ── PDF compilation ────────────────────────────────────────────────────────────

def compile_pdf(tex_content, stem, output_dir):
    """Write .tex to a temp dir, compile with pdflatex, copy PDF to output_dir."""
    os.makedirs(output_dir, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tex_path = os.path.join(tmp, f'{stem}.tex')
            with open(tex_path, 'w', encoding='utf-8') as f:
                f.write(tex_content)

            result = subprocess.run(
                ['pdflatex', '-interaction=nonstopmode',
                 f'-output-directory={tmp}', tex_path],
                capture_output=True, text=True, timeout=30
            )

            pdf_src = os.path.join(tmp, f'{stem}.pdf')
            if not os.path.exists(pdf_src):
                logging.error(f"pdflatex failed for {stem}:\n{result.stdout[-600:]}")
                return None

            pdf_dst = os.path.join(output_dir, f'{stem}.pdf')
            shutil.copy(pdf_src, pdf_dst)
            return pdf_dst

    except FileNotFoundError:
        logging.error(
            "pdflatex not found -- install with:\n"
            "  sudo apt-get install texlive-latex-base texlive-fonts-recommended texlive-latex-extra"
        )
        return None
    except Exception as e:
        logging.error(f"compile_pdf error for {stem}: {e}")
        return None


def count_pages(pdf_path):
    if not HAS_PYPDF or not pdf_path or not os.path.exists(pdf_path):
        return None
    try:
        return len(PdfReader(pdf_path).pages)
    except Exception:
        return None


def count_lines(pdf_path):
    """Non-empty text lines on the PDF: a proxy for how full the page is."""
    if not HAS_PYPDF or not pdf_path or not os.path.exists(pdf_path):
        return None
    try:
        return sum(len([l for l in (p.extract_text() or "").splitlines() if l.strip()])
                   for p in PdfReader(pdf_path).pages)
    except Exception:
        return None


def tailor_resume(client, analysis, custom_prompt, slug, output_dir, job_description=""):
    """
    Tailor the baseline resume (ResumeTemplate.tex) with small validated edits only.
    Layout guard: the result must stay on one page and use no more text lines than the
    untouched baseline; otherwise the edits that grew the most are reverted one by one.
    """
    with open(RESUME_TEMPLATE, "r", encoding="utf-8") as f:
        template = f.read()
    originals = resume_tailor.extract_sections(template)

    baseline_pdf = compile_pdf(template, f"_baseline_{slug}", output_dir)
    baseline_lines = count_lines(baseline_pdf)
    if baseline_pdf and os.path.exists(baseline_pdf):
        os.remove(baseline_pdf)

    edits = {}
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=3000, temperature=0.3,
            messages=[{"role": "user", "content": resume_tailor.build_prompt(
                originals, analysis, custom_prompt, job_description)}],
        )
        edits = parse_json_response(resp.content[0].text) or {}
    except Exception as e:
        logging.warning(f"Resume edit pass failed, using baseline resume: {e}")

    # Independent truthfulness check on every changed bullet
    pairs = resume_tailor.changed_pairs(originals, edits)
    rejected = []
    if pairs:
        try:
            verdict = parse_json_response(client.messages.create(
                model="claude-sonnet-4-6", max_tokens=1000, temperature=0,
                messages=[{"role": "user", "content": resume_tailor.build_verify_prompt(pairs)}],
            ).content[0].text) or {}
            rejected = [r for r in verdict.get("rejected", []) if isinstance(r, dict)]
        except Exception as e:
            # If the check can't run, don't trust the edits: fall back to the baseline wording
            logging.warning(f"Truthfulness check failed, keeping original wording: {e}")
            rejected = [{"id": i, "reason": "check unavailable"} for i, _, _ in pairs]
        edits = resume_tailor.drop_rejected(originals, edits, {r.get("id") for r in rejected})

    result, sources, changes = resume_tailor.apply_edits(originals, edits)
    changes += [{"id": r.get("id"), "status": "rejected (fact-check)", "reason": r.get("reason")} for r in rejected]

    for attempt in range(13):
        tex = resume_tailor.render(template, result)
        pdf = compile_pdf(tex, f"Resume_{slug}", output_dir)
        if pdf is None:
            return None, tex, changes
        pages, lines = count_pages(pdf), count_lines(pdf)
        fits = (pages is None or pages <= 1) and (
            baseline_lines is None or lines is None or lines <= baseline_lines)
        if fits:
            break
        logging.info(f"Resume layout guard: {pages} page(s), {lines} lines vs baseline {baseline_lines}")
        if not resume_tailor.revert_one(result, sources, originals, changes):
            logging.warning("Layout guard: no edits left to revert")
            break

    for c in changes:
        logging.info(f"Resume edit: {c}")
    return pdf, tex, changes


def trim_cover_to_one_page(data, slug, client, output_dir):
    """Compile cover letter. If > 1 page, ask Claude to shorten paragraphs and retry once."""
    cl = data.get('cover_letter', {})
    cover_tex = build_cover_tex(data)
    cover_pdf = compile_pdf(cover_tex, f'CoverLetter_{slug}', output_dir)

    pages = count_pages(cover_pdf)
    if pages is None or pages <= 1:
        return cover_pdf

    logging.info("Cover letter exceeds 1 page -- asking Claude to shorten")
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1500,
            messages=[{"role": "user", "content": f"""This cover letter is slightly over one page. Shorten each paragraph by removing unnecessary sentences while keeping the core story, voice, and logistics line intact. No em dashes. Return JSON only:

{{
  "date": "{cl.get('date', '')}",
  "paragraph_1": "...",
  "paragraph_2": "...",
  "paragraph_3": "..."
}}

Current paragraphs:
P1: {cl.get('paragraph_1', '')}
P2: {cl.get('paragraph_2', '')}
P3: {cl.get('paragraph_3', '')}"""}]
        )
        shorter = parse_json_response(resp.content[0].text)
        if shorter:
            updated = {**data, 'cover_letter': shorter}
            cover_tex = build_cover_tex(updated)
            cover_pdf = compile_pdf(cover_tex, f'CoverLetter_{slug}', output_dir)
            logging.info(f"Cover letter recompiled after shortening: {count_pages(cover_pdf)} page(s)")
    except Exception as e:
        logging.warning(f"Cover trim failed: {e}")

    return cover_pdf


# ── Job description fetcher ────────────────────────────────────────────────────

def fetch_job_description(url):
    headers = {"User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for selector in [
            ".job-description", ".description", "#job-description",
            "[data-testid='job-description']", ".jobsearch-jobDescriptionText",
            ".job__description", ".show-more-less-html__markup",
            "article", ".content",
        ]:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 200:
                return el.get_text(separator="\n", strip=True)
        body = soup.find("body")
        text = body.get_text(separator="\n", strip=True) if body else resp.text
        if len(text) > 200:
            return text[:6000]
    except Exception as e:
        logging.error(f"Failed to fetch JD from {url}: {e}")

    # Some sites (e.g. IBM careers) sit behind a JS bot-challenge (AWS WAF)
    # that returns an empty/near-empty page to a plain HTTP request -- a
    # real browser is the only thing that can pass the challenge.
    logging.info(f"Plain fetch came back empty for {url} -- retrying with headless browser")
    return fetch_job_description_browser(url)


def fetch_job_description_browser(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logging.error(
            "playwright not installed -- run: pip install playwright && playwright install chromium"
        )
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ))
            page.goto(url, timeout=25000, wait_until="load")
            page.wait_for_timeout(5000)
            text = page.inner_text("body")
            browser.close()
            return text[:6000] if text and len(text) > 200 else None
    except Exception as e:
        logging.error(f"Headless browser fetch failed for {url}: {e}")
        return None


def load_kb():
    with open(KB_PATH, "r") as f:
        return yaml.safe_load(f)


def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {}


# ── Core tailor ────────────────────────────────────────────────────────────────

def tailor(job_url, job_description=None, custom_prompt=""):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    kb     = load_kb()
    kb_str = yaml.dump(kb, default_flow_style=False)

    if not job_description:
        logging.info(f"Fetching JD from {job_url}")
        job_description = fetch_job_description(job_url)
    if not job_description:
        return None, None, {"error": "Could not fetch job description"}

    # ── Pass 1: Analyze JD ────────────────────────────────────────────────────
    logging.info("Pass 1: Analyzing JD")
    analysis = parse_json_response(client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        messages=[{"role": "user", "content": f"""Analyze this job description. Return JSON only.

{job_description[:5000]}

{{
  "role_title": "exact job title",
  "company": "company name",
  "company_slug": "lowercase-hyphenated for filenames",
  "must_have": ["3-5 required quals"],
  "nice_to_have": ["2-3 preferred quals"],
  "ats_keywords": ["12-15 keywords to include"],
  "tone": "formal | startup | technical | sales-focused"
}}"""}]
    ).content[0].text)
    if not analysis:
        analysis = {"role_title": "Role", "company": "Company", "company_slug": "company"}

    # ── Pass 2: Match experience ──────────────────────────────────────────────
    logging.info("Pass 2: Matching experience")
    matching = parse_json_response(client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1000,
        messages=[{"role": "user", "content": f"""Match the candidate's experience to this job. Only use facts from the KB.

KB:
{kb_str}

Role: {analysis.get("role_title")} at {analysis.get("company")}
Must have: {json.dumps(analysis.get("must_have", []))}
ATS keywords: {json.dumps(analysis.get("ats_keywords", []))}

Return JSON only:
{{
  "lead_experience": "which experience -- AIEA Lab or Montessori -- best fits this role and why",
  "cover_letter_angle": "one specific story that best fits this role",
  "gaps": [{{"gap": "requirement", "talking_point": "how to address it"}}]
}}"""}]
    ).content[0].text)
    if not matching:
        matching = {"cover_letter_angle": "", "gaps": []}

    # ── Pass 3: Generate structured content ───────────────────────────────────
    logging.info("Pass 3: Generating content")
    today = datetime.now().strftime("%B %d, %Y")
    custom_section = f"\nADDITIONAL INSTRUCTIONS FROM USER:\n{custom_prompt}\n" if custom_prompt else ""
    structured = parse_json_response(client.messages.create(
        model="claude-sonnet-4-6", max_tokens=3000,
        messages=[{"role": "user", "content": f"""Generate a tailored cover letter for {kb.get("personal", {}).get("name", "the candidate")} applying to {analysis.get("role_title")} at {analysis.get("company")}.

RULES:
- Only use real experience from the KB. Never fabricate metrics or responsibilities.
- Cover letter: 3 tight paragraphs, story-led, specific to this company.
- Voice: direct, enthusiastic, technically literate but people-first. No corporate fluff.
- No em dashes anywhere. Use commas or periods instead.
- Paragraph 3 must include a logistics line (location, availability, eager to connect).
{custom_section}
Context: {json.dumps(analysis)}
Matching: {json.dumps(matching)}
KB:
{kb_str[:3500]}

Return JSON only:
{{
  "cover_letter": {{
    "date": "{today}",
    "paragraph_1": "...",
    "paragraph_2": "...",
    "paragraph_3": "..."
  }},
  "gaps": [{{"gap": "...", "talking_point": "..."}}]
}}"""}]
    ).content[0].text)

    if not structured:
        logging.error("Pass 3 returned no parseable JSON")
        return None, None, analysis

    structured['company_slug'] = analysis.get('company_slug', 'company')
    slug = structured['company_slug']
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Build + compile ───────────────────────────────────────────────────────
    resume_pdf, resume_tex, resume_changes = tailor_resume(client, analysis, custom_prompt, slug, OUTPUT_DIR, job_description)
    cover_pdf      = trim_cover_to_one_page(structured, slug, client, OUTPUT_DIR)

    # Fallback: save .tex files if pdflatex unavailable
    if resume_pdf is None:
        path = os.path.join(OUTPUT_DIR, f'Resume_{slug}.tex')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(resume_tex)
        logging.warning(f"Resume PDF failed -- .tex saved to {path}")

    if cover_pdf is None:
        path = os.path.join(OUTPUT_DIR, f'CoverLetter_{slug}.tex')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(build_cover_tex(structured))
        logging.warning(f"Cover PDF failed -- .tex saved to {path}")

    logging.info(f"Tailor complete: resume={resume_pdf}, cover={cover_pdf}")
    return resume_pdf, cover_pdf, {**analysis, "gaps": structured.get("gaps", [])}


# ── Output helpers ─────────────────────────────────────────────────────────────

def format_gaps(gaps):
    if not gaps:
        return ""
    lines = ["GAPS TO PREPARE FOR:\n"]
    for i, g in enumerate(gaps, 1):
        lines.append(f"{i}. {g.get('gap', '')}")
        lines.append(f"   Talking point: {g.get('talking_point', '')}\n")
    return '\n'.join(lines)


async def send_to_discord(channel_id, job_url, resume_pdf, cover_pdf, analysis):
    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))

            role    = analysis.get("role_title", "Role")
            company = analysis.get("company", "Company")
            gaps    = format_gaps(analysis.get("gaps", []))

            files = []
            if resume_pdf and os.path.exists(resume_pdf):
                files.append(discord.File(resume_pdf, filename=f"Resume_{company}.pdf"))
            if cover_pdf and os.path.exists(cover_pdf):
                files.append(discord.File(cover_pdf, filename=f"CoverLetter_{company}.pdf"))

            error = analysis.get("error")
            if error:
                await channel.send(f"Tailor failed: {error}")
            else:
                await channel.send(
                    content=f"**{role} @ {company}**\n{job_url}",
                    files=files or discord.utils.MISSING
                )

                if not files:
                    await channel.send(
                        "PDF generation failed -- check /var/log/jobscout_tailor.log for details.\n"
                        "If pdflatex isn't installed, run on VPS:\n"
                        "`sudo apt-get install texlive-latex-base texlive-fonts-recommended texlive-latex-extra`\n"
                        f".tex files saved to `{OUTPUT_DIR}`"
                    )

            if gaps:
                for chunk in [gaps[i:i+1800] for i in range(0, len(gaps), 1800)]:
                    await channel.send(f"```\n{chunk}\n```")

        except Exception as e:
            logging.error(f"Discord send error: {e}")
        finally:
            await bot.close()

    await bot.start(BOT_TOKEN)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("job_url", help="URL of the job posting (use 'no-url' when passing --jd-file)")
    parser.add_argument("channel_id", nargs="?", default=None, help="Discord channel ID to send results to")
    parser.add_argument("--prompt", default="", help="Custom instructions for tailoring")
    parser.add_argument("--jd-file", default="", help="Path to a text file containing the job description (skips URL fetch)")
    args = parser.parse_args()

    job_url       = args.job_url if args.job_url != "no-url" else None
    channel_id    = args.channel_id
    custom_prompt = args.prompt

    # Load job description from file if provided (e.g. extracted from PDF)
    job_description = None
    if args.jd_file and os.path.exists(args.jd_file):
        with open(args.jd_file, encoding="utf-8") as f:
            job_description = f.read().strip()
        logging.info(f"Loaded JD from file: {args.jd_file} ({len(job_description)} chars)")

    logging.info(f"Starting tailor: {job_url or '(from file)'} (custom: {custom_prompt[:100]})")
    resume_pdf, cover_pdf, analysis = tailor(job_url or "no-url", job_description=job_description, custom_prompt=custom_prompt)

    if channel_id:
        asyncio.run(send_to_discord(channel_id, job_url, resume_pdf, cover_pdf, analysis))
    else:
        # CLI mode: copy to RESUME_SYNC_PATH if configured, else leave in OUTPUT_DIR
        dest = RESUME_SYNC_PATH if RESUME_SYNC_PATH and os.path.isdir(RESUME_SYNC_PATH) else OUTPUT_DIR
        slug = analysis.get("company_slug", "company")

        for src, name in [
            (resume_pdf, f"Resume_{slug}.pdf"),
            (cover_pdf,  f"CoverLetter_{slug}.pdf"),
        ]:
            if src and os.path.exists(src):
                final = os.path.join(dest, name)
                if os.path.abspath(src) != os.path.abspath(final):
                    shutil.copy(src, final)
                print(f"Saved: {final}")
            else:
                tex = os.path.join(OUTPUT_DIR, name.replace('.pdf', '.tex'))
                if os.path.exists(tex):
                    print(f"PDF unavailable -- LaTeX saved: {tex}")

        gaps = format_gaps(analysis.get("gaps", []))
        if gaps:
            print(f"\n{gaps}")

    logging.info(f"Done: {job_url}")


if __name__ == "__main__":
    main()
