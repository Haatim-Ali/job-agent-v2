# Job Agent — Claude Code Instructions

## Stack
- Next.js 16 (App Router, TypeScript)
- Supabase (PostgreSQL)
- Anthropic Claude API
- Tailwind CSS + shadcn/ui components
- Deployed on Vercel (auto-deploys on `git push origin master`)

## Dev

```bash
export PATH="/c/Program Files/nodejs:$PATH"
npm run dev    # local dev server at http://localhost:3000
npm run build  # production build (requires Vercel env vars or .env.local)
```

## Testing

```bash
npm test           # run all tests (vitest)
npm run test:watch # watch mode
```

See [TESTING.md](./TESTING.md) for conventions.

### Test expectations
- 100% test coverage is the goal — tests make vibe coding safe
- When writing new functions in `lib/`, write a corresponding test in `test/`
- When fixing a bug, write a regression test
- When adding error handling, write a test that triggers the error
- When adding a conditional (if/else), write tests for both paths
- Never commit code that makes existing tests fail
- Mock `@anthropic-ai/sdk` and `@supabase/supabase-js` — never make real API calls in tests

## Key Files
- `app/api/jobs/ingest/route.ts` — webhook for scraper (POST with `x-api-key` header)
- `app/api/chat/route.ts` — Claude streaming chat
- `lib/claude.ts` — system prompt builder + `buildSystemPrompt()`
- `lib/supabase.ts` — DB client + TypeScript types
- `components/ui/` — shadcn/ui components (Button, Card, Input, Textarea, Badge)

## Memory

Persistent memory for this project lives in `C:\Users\joshs\job-agent-memory\`.

At the start of each session, read:
- `job-agent.md` — project context and search criteria
- `rejection-patterns.md` — recent dismissals from Discord (last 20 rows)

Update `job-agent.md` when priorities change, new decisions are made, or active work shifts.
The bot on the VPS appends to `rejection-patterns.md` automatically when you dismiss a job with a reason.

## Living architecture doc

`docs/job-agent-explained.md` → `docs/Job Agent Explained.pdf` explains how the whole system works.
Whenever code, config, deployment or architecture changes (or you learn something significant about
the internals), update the relevant section of the markdown, add a dated Changelog entry, and re-render:

```bash
cd docs && ~/.claude/skills/gstack/make-pdf/dist/pdf generate --quiet --cover --toc --no-confidential \
  --author "Haatim Ali" --title "Job Agent Explained" job-agent-explained.md "Job Agent Explained.pdf"
```
