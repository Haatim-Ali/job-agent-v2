# Job Agent: Claude Code instructions

Haatim's internship-search bot. **Everything that runs is in `job-scout/`** (Python) on a
Hostinger VPS. The Next.js app (`app/`, `components/`, `lib/`, `supabase/`, `test/`,
`package.json`) is the original shared project's web dashboard and is **not used** by this
deployment; don't edit it for job-bot work.

Full explanation of every component: `docs/job-agent-explained.md` (rendered to
`docs/Job Agent Explained.pdf`). Read the relevant part before changing something.

## Where things live

- **Repo:** branch `vps`, pushed to `Haatim-Ali/job-agent-v2` (git remote `private`; renamed from
  `job-agent-private` and made **public** on purpose, it's on Haatim's resume). Because it's public,
  never commit secrets, personal data or the VPS address (use the `jobagent-vps` SSH alias).
  `origin` is an old public fork (`Haatim-Ali/job-agent`) and `upstream` the collaborator's
  original (`jsho0/job-agent`): don't push job-bot work there.
- **VPS:** `ssh jobagent-vps` (an alias in `~/.ssh/config`; the address is not in git), code in `~/job-agent` (a git checkout of `job-agent-v2`,
  remote `origin` = that repo via deploy key), venv at `job-scout/venv`.
  - Scraper: crontab, every 30 min with `flock -n /tmp/jobscraper.lock`
  - Bot: systemd service `jobscout-bot` (`sudo systemctl restart jobscout-bot`)
  - Backups: `backup_db.py` nightly at 3:15 to `~/backups` (14 kept)
  - Logs: `/var/log/jobscraper.log`, `jobscout_bot.log`, `jobscout_tailor.log`,
    `job-scout/apply_prep.log`; rotated by `/etc/logrotate.d/jobscout`
- **Not in git (personal or secret):** `job-scout/.env`, `job-scout/application_answers.yaml`
  (address, legal name, citizenship, demographics; template in `.example`), `jobs.db`,
  `output/`, `applications/` (packets copied to the Mac).

## Working on it

```bash
cd job-scout
../.venv/bin/python -m pytest -q --ignore=test_discord.py --ignore=test_linkedin.py   # on the Mac
bash ../deploy.sh   # commit first; pushes, pulls on the VPS, runs tests there, rolls back on failure, restarts the bot
```

- CI (`.github/workflows/job-scout-tests.yml`) runs the same tests on every push to `vps`.
- `test_discord.py` and `test_linkedin.py` are manual scripts that hit real services, not tests.
- Write a test for every new function and a regression test for every bug fixed.
- Mock Anthropic, Discord, Google and HTTP calls in tests; never make real API calls.
- After any change to code, config, deployment or architecture: update
  `docs/job-agent-explained.md` (the relevant section plus a dated Changelog entry) and re-render:

```bash
cd docs && ~/.claude/skills/gstack/make-pdf/dist/pdf generate --quiet --cover --toc --no-confidential \
  --author "Haatim Ali" --title "Job Agent Explained" job-agent-explained.md "Job Agent Explained.pdf"
```

## Rules

- **No form filling (since 2026-10-04).** Haatim applies himself with each site's autofill.
  The bot's job is a tailored resume + cover letter for every alert (`tailor_alerts`), and on
  request ("resume for <company>" or a pasted job link). Packet / fill / watcher / remote-filler
  code is disabled (`application_packets: false`); don't re-enable it unless asked.
- Ask before ticking any agreement or policy acknowledgment on an application.
- Don't guess facts on applications (address, GPA, dates): leave them flagged for Haatim.
- Back up files on the VPS before changing anything outside git (crontab, systemd, /etc).
