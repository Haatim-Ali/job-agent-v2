# Fork Personalization + Tech Audit — Design Doc

**Author:** Haatim Ali
**Date:** 2026-07-25
**Status:** Approved plan, pending execution

## Context

This repo is forked from a friend's (Josh Sachs) personal job-search tool. It has
two hardcoded problems: (1) it's tuned to Josh's identity, target roles, and
infra (VPS path, Discord IDs, Vercel URL), and (2) it ships a full Next.js +
Supabase + Vercel web app (kanban board, AI chat, profile page) that isn't part
of the flow actually wanted here.

## Target flow (confirmed)

```
LinkedIn (scraper.py, every 30 min via cron)
   → Claude relevance filter
   → Discord #job-alerts channel (rich embed: fit score, freshness, applicant count)
        ✅ reaction → tailor.py generates tailored resume + cover letter PDFs, posts to Discord
        📨 reaction → mark applied (local sqlite, !tracker / !stats commands)
        ❌ reaction → dismiss + ask why (learns rejection patterns)
   #requests channel → paste a job URL/PDF → bot asks what you want, tailors on demand
```

No web app. No Vercel. No Supabase. Everything runs as one systemd service +
one cron job on the existing VPS (`/home/Haatim/job-agent`).

## Decisions locked in this session

| Decision | Choice |
|---|---|
| Web app | **Dropped entirely** — Discord-only architecture |
| Target roles | SWE/AI-ML internships, applied-ML/agentic-AI roles, PM internships, broadly tech-adjacent |
| Locations | Bay Area + Remote |
| Pay floor | $25/hr minimum |
| Company/industry exclusions | None — cast the widest net |
| Cover letter voice | Sensible default (direct, story-led, technically literate, matches resume voice) — refine later with a real writing sample |
| Resume source | `templates/resume_template.tex` (already contains real content: Haatim Ali, UCSC CS, AIEA Lab research, Kode With Klossy, Golden Harvest Montessori, SASE treasurer, Pathwise hackathon project) |
| VPS | user `Haatim`, path `/home/Haatim/job-agent` (matches current checkout) |
| Discord | Not set up yet — needs a from-scratch walkthrough (server, bot app, channels, IDs) |
| GitHub repo URL | Not needed — no Vercel deploy button anymore, README will use generic "your fork" language |

## Full audit — every hardcoded-to-Josh reference found

| File | Issue | Planned action |
|---|---|---|
| `app/`, `components/`, `lib/supabase.ts`, `lib/supabase-server.ts`, `lib/claude.ts`, `lib/utils.ts` | Entire web app | **Delete** |
| `supabase/schema.sql`, `supabase/migration_add_dismissed.sql` | Kanban DB schema | **Delete** |
| `next.config.ts`, `tailwind.config.ts`, `postcss.config.mjs`, `tsconfig.json`, `vitest.config.ts`, `package.json`, `package-lock.json`, `next-env.d.ts` | Next.js tooling | **Delete** |
| `test/*.test.ts`, `test/setup.ts`, `TESTING.md` | Vitest/web tests | **Delete** (Python tests already exist: `job-scout/test_*.py`) |
| `.env.local.example` | Web app env vars | **Delete** |
| `app/api/jobs/ingest`, `lib` sync logic, `INGEST_URL`/`INGEST_API_KEY` refs in `bot.py`/`scraper.py` | Web sync plumbing | **Delete/strip** |
| `job-scout/candidate_profile.yaml` | Josh's name, SDR/BDR sales targeting | **Rewrite** with locked-in decisions above |
| `job-scout/scraper_config.json` | Sales search terms, Bay Area sales watchlist | **Rewrite** with SWE/AI-ML/PM terms |
| `job-scout/tailor.py:339,364` | Hardcodes `"Josh's experience"` / `"Josh Sachs"` in Claude prompts | **Fix** — read name from `candidate_profile.yaml`/`experience_kb.yaml` dynamically |
| `job-scout/scraper.py:251` | Hardcodes `"Josh has recently passed on these roles..."` | **Fix** — same, dynamic name |
| `job-scout/bot.py`, `job-scout/scraper.py` | Default `INGEST_URL = "https://job-agent-henna.vercel.app"` | **Delete** (no web app to sync to) |
| `job-scout/job-scout-bot.service` | `User=josh`, `/home/josh/job-agent/...` | **Rewrite** → `User=Haatim`, `/home/Haatim/job-agent/...` |
| `deploy.sh` | `VPS="josh@100.117.198.30"` | **Flag for you to edit** — needs your VPS's actual SSH address, not something to guess/paste in chat |
| `README.md` | Vercel deploy button → `github.com/jsho0/job-agent`, two-part web+bot instructions | **Rewrite** — Discord-bot-only quick start |
| `CLAUDE.md` | `Deployed on Vercel`, memory path `C:\Users\joshs\job-agent-memory\` (Windows) | **Rewrite** — Linux path, Discord-only stack description |
| `docs/designs/phase2-interview-intelligence.md` | "Hard deadline: Josh graduates UCSC June 2026" — describes the dropped web app's interview-prep chat feature | **Archive/delete** — feature doesn't exist in Discord-only build |
| `job-scout/run_pathway.py`, `job-scout/run_uss.py` | Josh's own scratch test scripts (hardcoded example job postings for a marketing internship and a sales AE role — don't match your target roles) | **Delete** — ad hoc dev scripts, not part of the pipeline |
| `job-scout/experience_kb.yaml` (doesn't exist yet, only `.example`) | N/A | **Create** — populated from `templates/resume_template.tex` |
| `.gitignore` | Already correctly excludes `.env`, `job-scout/.env`, venv, `*.db` | **No change needed** |

## Content plan for the 3 personalization files

**`experience_kb.yaml`** — built from `templates/resume_template.tex`:
- personal: Haatim Ali, haatimta@gmail.com, San Jose CA, (510) 468-7997, linkedin.com/in/haatim-ali, github.com/Haatim-Ali
- education: UC Santa Cruz, B.S. CS, Expected March 2028; coursework (Calc I-II, Linear Algebra, Statistics, CSE 20/30, Computer Systems & Assembly); involvements (AKPsi Historian, AIEA Lab, Grappling Club, GDG on Campus)
- experience: AIEA Lab research assistant (CARLA/Vista, CNN on Nautilus K8s, 5 RL algorithms, TensorBoard), Golden Harvest Montessori website dev, Kode With Klossy AI/ML instructor assistant, SASE Treasurer
- projects: Pathwise (autonomous financial planning agent, NVIDIA Nemotron 49B via NIM, Hack-a-Claw 2026)
- cover_letter_style: default voice (direct, story-led, one page)

**`candidate_profile.yaml`** — name Haatim Ali, background "UCSC CS student (Expected March 2028) with AI/ML research + hackathon experience", target_roles covering SWE/AI-ML internships + applied-ML/agentic-AI + PM internships + general tech, Bay Area + Remote, $25/hr+ min, no disqualifying industries.

**`scraper_config.json`** — search_terms for SWE intern / ML engineer intern / AI engineer intern / applied AI intern / product management intern / research intern; locations San Jose, Santa Cruz, SF, Remote; no exclude_industries; watchlist left open (or general big tech + AI labs if you want it later).

## What's NOT touched

- `job-scout/scraper.py`, `job-scout/bot.py`, `job-scout/tailor.py` core logic (only the hardcoded-name lines)
- `job-scout/test_discord.py`, `test_linkedin.py`, `test_scraper.py`
- `templates/resume_template.tex` (source of truth, untouched)
- Cron cadence (every 30 min — already matches what you asked for)

## Assignment (do this next)

1. **Discord setup** (nothing exists yet) — I'll walk you through:
   - Create an application at discord.com/developers → add a Bot → enable
     Message Content Intent + Server Members Intent → copy the bot token
   - Create a Discord server (or use an existing private one) with two
     channels: `#job-alerts` and `#requests`
   - OAuth2 → URL Generator → scope `bot`, permissions `Send Messages`,
     `Read Message History`, `Add Reactions`, `Attach Files` → invite the bot
   - Enable Developer Mode in Discord settings → right-click each channel and
     yourself → copy IDs → these become `DISCORD_CHANNEL_ID`,
     `REQUESTS_CHANNEL_ID`, `DISCORD_USER_ID` in `job-scout/.env`
2. Send me the token + 3 IDs (or just confirm you've got them saved into
   `job-scout/.env` — you don't need to paste the token itself into chat).
3. Say **"go"** and I'll execute the actual repo changes from the audit table
   above: delete the web app, fix the hardcoded name in `tailor.py`/`scraper.py`,
   write real `candidate_profile.yaml`/`scraper_config.json`/`experience_kb.yaml`,
   fix `job-scout-bot.service`, rewrite `README.md`/`CLAUDE.md`, and flag the
   `deploy.sh` VPS line for you to fill in yourself.
