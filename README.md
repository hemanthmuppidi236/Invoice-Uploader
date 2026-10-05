# Ferrocrete Invoice Processor

Vendor invoices arrive in a Google Drive folder. This app extracts them,
suggests a cost code, routes the suggestion to the project engineer who knows
the work, requires an approver, and only then lets a Claude in Chrome session
push the bill into BuilderTrend.

The point is where cost coding happens. Today accounting guesses the code
after the fact; this moves that decision to the people who poured the concrete
and adds an approval step behind it. **Nothing reaches BuilderTrend without a
human `reviewed` stamp and a human `approved` stamp**, and the app never drives
the browser itself — Linda always starts the session.

## Stack

Mirrors the Ferrocrete pay app deliberately: same auth, same role pattern,
same API client, same design system.

| Layer | Choice |
|---|---|
| Frontend | Next.js 14 App Router, all-client pages, on Vercel |
| Backend | FastAPI on Render |
| Data | Supabase — Postgres, Auth (Google OAuth, `@ferrocretebuilders.com` only), Storage |
| AI | Claude API (`claude-opus-5`) for invoice extraction and mix design parsing |
| Email | Gmail API, same single-user OAuth setup as the pay app |
| Drive | Google Drive API for polling and filing |
| Design | CHAD design system — EB Garamond + IBM Plex Mono, gold `#b8852a`, Ferrocrete red `#d53b34`, glass cards, dark mode |

The backend owns roles. The frontend reads them from `GET /me`. Every write
goes through FastAPI behind `require_role`; the browser has read-only RLS
policies and **no** write policies at all.

## Repository layout

```
backend/            FastAPI service
  app/api/          route modules, one per resource
  app/core/         auth, config, storage, audit, Claude client, mix parser
  app/schemas/      Pydantic request/response models (extra="forbid" on writes)
  tests/            unit tests, no network or DB required
frontend/           Next.js app
  src/app/(app)/    authenticated screens
  src/components/   shared UI
  src/lib/          api client, types, auth hook
migrations/         numbered SQL, applied in order in the Supabase SQL editor
scripts/            one-off generators and seed templates
docs/               setup and phase notes
```

## Current state: Phase 2

**Phase 0** — repo, auth, roles, design shell, schema and migrations, seeded
reference data, project onboarding with mix design parsing.

**Phase 1** — Drive polling, PDF storage, AI extraction and cost-code
suggestion, flagged triage.

**Phase 2** — assignment, review, approval, the audit trail, and both daily
emails.

**Phase 3** — the upload queue the Chrome session reads, the `mark-uploaded`
guard, and the Drive filing that follows it.

**Phase 4** — White Cap combined-file splitting, duplicate detection that
survives a re-scan, and the metrics page that says whether the AI is earning
its trust.

Built and working:

- Google OAuth login, domain-restricted, with the backend as the authority on roles
- Five roles (`admin`, `accountant`, `approver`, `pe`, `viewer`), held in combination
- Full invoice schema, 236 BuilderTrend cost codes, and the SOP §6 vendor mapping seeded
- Project onboarding: a mix design submittal read by Claude into a confirmable yardage table with a proposed cost code per mix
- Drive intake: idempotent polling, PDF storage, and a row claimed before the download so overlapping cron runs cannot double-ingest
- AI extraction per §8: the full authority order (comment → mix number → description → vendor default), phase inference from the last 30 days of approved pours, and the comment-versus-mix conflict rule
- Flagged triage grouped by reason, with the inline fixes each reason needs
- The full review workflow: assign (single and bulk), reassign, mark reviewed, approve, reject with a reason — one state-machine guard behind all of them
- Both daily emails, in the app's design language, sent once per person per day
- Invoice list with the role-aware "Your court" queue, stat cards, and filters
- Invoice detail: PDF beside the form, the AI rationale with one-click alternative chips, a cost split that auto-saves, the workflow panel, and the audit trail
- `/admin` for cost codes, vendors, users and roles, and the distribution list
- The upload queue: the BuilderTrend field values computed server-side, with blockers separated from warnings, and the §12 guard plus double-save protection on `mark-uploaded`
- Drive filing: the vendor folder's own naming convention inferred from what is already in it, the original moved to `Uploaded/`, and never a delete or a created folder
- `/uploads`: the approved queue as a per-invoice form preview, the retry for a failed filing, and the last sessions' results
- White Cap combined files split into one invoice per page, with the combined original held in the intake folder until every page is filed
- Duplicate detection that survives a re-scan: near-amount matching, normalised invoice numbers, a date window, and a byte-identical PDF check that outranks all of them
- `/metrics`: AI acceptance by vendor, cost code, and confidence band — including whether the confidence number predicts anything at all
- Element keywords editable in `/admin`, which is the fix `/metrics` points at

Still outstanding from §12: Sentry and structured JSON logging. Every 500
carries an `error_id` that also appears in the Render logs.

## Getting started

Setup, and what still has to be pasted in from the kickoff document, is in
**[docs/PHASE_0.md](docs/PHASE_0.md)**. Drive credentials and how the cost
code gets chosen are in **[docs/PHASE_1.md](docs/PHASE_1.md)**. The workflow,
Gmail setup, and the cron schedules are in
**[docs/PHASE_2.md](docs/PHASE_2.md)**. The upload and filing half is in
**[docs/PHASE_3.md](docs/PHASE_3.md)**, and the contract the Chrome session
itself works from is **[docs/CHROME_SESSION.md](docs/CHROME_SESSION.md)**,
which merges that contract with the BuilderTrend SOP into one playbook. The White
Cap splitter, duplicate scoring, and metrics are in
**[docs/PHASE_4.md](docs/PHASE_4.md)**.

The short version:

```bash
# 1. Create a Supabase project, then run migrations/001 through 005 in order
#    in the SQL editor. Create the `invoices` and `mix-designs` Storage
#    buckets and keep both private.

# 2. Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in Supabase keys and ANTHROPIC_API_KEY
.venv/bin/uvicorn app.main:app --reload --port 8000

# 3. Frontend
cd frontend
npm install
cp .env.example .env.local  # fill in the same Supabase project + API URL
npm run dev
```

Then open `http://localhost:3000`. `GET /health` on the backend reports which
integrations are actually wired up, which is usually the fastest way to find a
missing key.

## Tests

```bash
cd backend && .venv/bin/python -m pytest tests -q
cd frontend && npm run typecheck && npm run build
```

## Guardrails

These are enforced in code, not just documented:

- **A human reviews and a human approves before anything reaches BuilderTrend.** The `approved` state is the only one the Chrome session reads, and the agent key is never accepted on the endpoints that produce it.
- **The app never creates a Sub/Vendor, cost code, project, or Drive folder.** Reference data records what already exists elsewhere. Anything unmatched is flagged for a person.
- **No Drive file is ever deleted.** Originals are moved to `Uploaded/`.
- **Onboarding is a gate, not a label.** A project with no project engineer, or a concrete job with no confirmed mix design, cannot be onboarded, and its invoices are flagged rather than routed.
- **Nothing is silently skipped.** Every intake and extraction failure lands the invoice in `flagged` with the error text. A skipped invoice is an unpaid vendor, and nobody finds that out from an empty list.
- **A retry never discards a human's judgment.** Re-running the AI is refused once a reviewer has signed off, because it rewrites the line items and the cost split wholesale.
- **The person who acted is the person who was asked.** Review is limited to the assigned reviewer, approval to the assigned approver. Another holder of the same role is refused; reassignment is the supported path, and it is recorded.
- **An unbalanced cost split cannot be approved.** The rows have to sum to the invoice total, checked at review and re-asserted at approval — the last gate before the only state the Chrome session reads.
- **Every transition writes an audit row** with the actor, a UTC stamp, the states either side, and a field-level diff of what changed.
- **A PATCH can never move a status.** Write schemas use `extra="forbid"` and omit every status and workflow timestamp, so a smuggled field is a 422. Status moves only through the named transition endpoints.
- **Secrets live in the environment only**, behind a typed loader that fails on boot rather than on the first request.
- **A kill switch** (`JOBS_PAUSED`) stops Drive polling and both email jobs without a deploy.

## Reference documents

- `BuilderTrend Invoice Upload - Handover SOP.md` — the manual process this automates around. Section numbers are cited throughout the code.
- `PAY_APP_OVERVIEW.md` — the sibling app whose stack, auth, and design this copies.
- `INVOICE_APP_PROMPT.md` — the build specification. Section numbers (§7.0, §8, §12) in code comments refer to it.
