# Job Agent (Haatim's deployment)

An internship-search bot that runs 24/7 on a VPS:

- **Finds jobs** every 30 minutes on LinkedIn and directly on company job boards (Greenhouse, Lever, Ashby, Amazon), filters them with cheap rules, then scores the survivors with Claude Haiku.
- **Alerts on Discord** (#job-alerts). React 👍 for a tailored resume + cover letter, ✅ to log an application, ❌ to dismiss with a reason the scorer learns from.
- **Prepares application packets** for company-board jobs (and LinkedIn jobs it can find on the company's own board): tailored resume and cover letter, the form's real questions, and a drafted, validated answer for each. Notices go to #applications.
- **Fills applications** in Chrome from your Mac with the `/submit-applications` Claude Code skill. You do the CAPTCHA and click Submit; it logs the application, posts ✅ and updates the Google Sheet tracker.

```
cron (30 min) → scraper.py → filters → Claude Haiku score → Discord #job-alerts
                     │                                          │ reactions
                     └→ apply_prep.py → packet → #applications   └→ bot.py → jobs.db + Google Sheet
                                           │
                     your Mac: /submit-applications → Chrome form → you click Submit
```

**How it all works, in detail:** [`docs/job-agent-explained.md`](docs/job-agent-explained.md) (PDF: `docs/Job Agent Explained.pdf`), including a glossary of every technical term.

## Layout

| Path | What |
|---|---|
| `job-scout/` | Everything that runs: scraper, bot, tailoring, packets, form filling, tests |
| `job-scout/requirements.txt` | Pinned Python dependencies |
| `docs/` | Job Agent Explained |
| `deploy.sh` | Deploy to the VPS (tests first, rolls back on failure) |
| `.github/workflows/` | CI: runs the tests on every push |
| `app/`, `components/`, `lib/`, `supabase/`, `test/` | The original project's Next.js dashboard. **Not used** by this deployment |

## Common tasks

```bash
# Run the tests (Mac)
cd job-scout && ../.venv/bin/python -m pytest -q --ignore=test_discord.py --ignore=test_linkedin.py

# Deploy (commit first)
bash deploy.sh

# On the VPS
ssh Haatim@2.25.82.228
cd ~/job-agent/job-scout
venv/bin/python apply_prep.py --list              # application queue
venv/bin/python apply_prep.py --url <posting>     # build a packet now
tail -f /var/log/jobscraper.log                   # watch a scraper run
sudo systemctl restart jobscout-bot               # restart the bot
```

## Setting up from scratch

1. A Linux server with Python 3.13 and `texlive` (`pdflatex`).
2. `git clone` this repo, then in `job-scout/`: `python3 -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/playwright install chromium`.
3. Copy `.env.example` → `.env` (Discord bot token, webhook, channel IDs, Anthropic key, Google service account) and `application_answers.yaml.example` → `application_answers.yaml`. Edit `candidate_profile.yaml`, `experience_kb.yaml`, `scraper_config.json` and `ResumeTemplate.tex` for the candidate.
4. Crontab (scraper every 30 min with `flock`, nightly `backup_db.py`), the systemd unit `jobscout-bot.service`, and `/etc/logrotate.d/jobscout`. The exact lines are in Part 3 of the docs.

This repository started as a fork of [jsho0/job-agent](https://github.com/jsho0/job-agent) (co-developed); the VPS copy has since diverged into the system described above.
