#!/bin/bash
# Deploy the job bot to the VPS from the private GitHub repo.
# Run from your Mac:  bash deploy.sh
#
#   1. push the local vps branch to the private repo
#   2. on the VPS: pull it, run the tests
#   3. tests fail -> roll back to the previous commit and stop
#      tests pass -> restart the Discord bot (the scraper picks up new code on its next cron run)
set -euo pipefail

VPS="Haatim@2.25.82.228"
BRANCH="vps"

cd "$(dirname "$0")"
if [ -n "$(git status --porcelain)" ]; then
  echo "Uncommitted changes: commit them first so the VPS runs exactly what's in git."
  exit 1
fi

echo "Pushing $BRANCH to the private repo..."
git push -q private "$BRANCH"

ssh "$VPS" BRANCH="$BRANCH" bash -s <<'REMOTE'
set -euo pipefail
cd ~/job-agent
PREV=$(git rev-parse HEAD)
git fetch -q origin "$BRANCH"
git merge -q --ff-only "origin/$BRANCH"
NEW=$(git rev-parse HEAD)
if [ "$PREV" = "$NEW" ]; then echo "VPS already up to date ($NEW)"; exit 0; fi
echo "VPS: $PREV -> $NEW"

cd job-scout
if ! venv/bin/python -m pytest -q --ignore=test_discord.py --ignore=test_linkedin.py; then
  echo "TESTS FAILED on the VPS: rolling back to $PREV"
  cd .. && git reset -q --hard "$PREV"
  exit 1
fi

sudo systemctl restart jobscout-bot
sleep 3
echo "Bot: $(systemctl is-active jobscout-bot)"
REMOTE
echo "Deployed."
