# Phase 0 — setup and gate

Phase 0 covers repo, auth, roles, design shell, schema and migrations, seeded
reference data, and the project onboarding screen with mix design parsing.

The Phase 0 gate from the build spec is: **log in as each role, onboard A
Street Flats from the CMD-01 submittal, and see its mix table with cost codes
mapped.** Everything the app needs for that is built. What is still missing is
data — see "Outstanding kickoff data" below.

---

## 1. Create the Supabase project

A **new** project, separate from the pay app. The two apps then have
independent schemas, Storage buckets, and migration histories.

1. Create the project.
2. **Authentication → Providers → Google**: enable it, and restrict sign-ups to
   `ferrocretebuilders.com`. This is the real domain restriction; the backend
   re-checks the claim but Supabase is what stops the account existing.
3. **SQL Editor**: run the migrations in order. Each is idempotent enough to
   re-run, but run them once, in sequence, and read the output.

   | File | What it does |
   |---|---|
   | `migrations/001_initial_schema.sql` | All twelve tables, indexes, triggers, RLS |
   | `migrations/002_seed_cost_codes.sql` | 236 BuilderTrend cost codes with element keywords |
   | `migrations/003_seed_vendors.sql` | The SOP §6 vendor name mapping |

   Migration 003 ends with a check that raises if the White Cap and Cefali
   default cost codes did not resolve — if you see that exception, 002 was not
   applied first.

4. **Storage**: create two buckets and leave both **private**.

   | Bucket | Holds |
   |---|---|
   | `invoices` | The invoice PDF pulled off Drive, one per invoice row |
   | `mix-designs` | Each project's mix design submittal |

   Private matters: the frontend reads PDFs through short-lived signed URLs.
   A public bucket would put every vendor invoice on a guessable URL.

5. **Project Settings → API**: copy the project URL, the `anon` key, and the
   `service_role` key.

---

## 2. Seed the roster

Nobody can do anything but read until they have a role. Two ways in:

**Option A — from the app.** Sign in yourself first (you will land as
`viewer`), then promote your own row directly in the Supabase SQL editor:

```sql
UPDATE app_users
   SET role = ARRAY['admin']::TEXT[]
 WHERE email = 'your.email@ferrocretebuilders.com';
```

From then on use `/admin → Users and roles` for everyone else.

**Option B — seed everyone up front.** Copy
`scripts/seed_users.sql.example` to `migrations/004_seed_users.sql`, fill in
the real email addresses, and apply it. It refuses to run while any
`REPLACE-` placeholder is still in place.

Either way, the signup trigger claims a pre-seeded row by email on that
person's first Google sign-in and re-points it at their real auth id, keeping
the roles you granted. Every foreign key onto `app_users(id)` is
`ON UPDATE CASCADE` so that re-point is safe.

### Roles

| Role | Who | Can |
|---|---|---|
| `accountant` | Linda | Everything below plus assign, reassign, run intake, trigger uploads, manage reference data |
| `approver` | Raz, Linda, Shant | Approve, reject, view all |
| `pe` | Project engineers | Review invoices assigned to them, edit suggested fields, mark reviewed, view all |
| `admin` | Hemanth | Everything, plus renumber, void, delete, and user management |
| `viewer` | anyone else | Read only — the default for a new signup |

A person holds several at once; Linda is `accountant` **and** `approver`.

---

## 3. Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. The three Supabase values are required and the process
refuses to start without them. `ANTHROPIC_API_KEY` is required for mix design
parsing — without it `/projects/parse-mix-design` returns a 503 saying so
rather than failing obscurely.

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

`GET /health` reports which integrations are live:

```json
{
  "status": "ok",
  "integrations": { "claude": true, "drive": false, "email": false, "agent_auth": false },
  "jobs_paused": false
}
```

`drive`, `email`, and `agent_auth` are expected to be `false` in Phase 0 —
nothing uses them yet.

### Render

Deploy `backend/` as a Docker service. Set every `.env` value as an
environment variable, plus `CORS_ORIGINS` pointing at the Vercel URL.
Generate the agent key now even though nothing uses it until Phase 3:

```bash
openssl rand -hex 32
```

Leaving `AGENT_API_KEY` unset denies the agent, which is the safe default.

---

## 4. Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

`.env.local` needs the same Supabase project URL and `anon` key the backend
uses, plus `NEXT_PUBLIC_API_URL` pointing at the backend.

### Vercel

Import `frontend/` as the root directory. Set the three
`NEXT_PUBLIC_*` variables. Then add the Vercel URL to the backend's
`CORS_ORIGINS` and to Supabase's **Authentication → URL Configuration →
Redirect URLs** as `https://your-app.vercel.app/auth/callback`.

---

## 5. Outstanding kickoff data

The build spec's §15 paste block was never filled in. These are the gaps, and
what each one blocks:

| Missing | Blocks | Where it goes |
|---|---|---|
| **Project list** — project number, BT job name, `jobId`, address, PE, default approver, `old`/`active`, Drive folder name | Everything. Nothing routes without at least one onboarded project. | `/projects/new`, one at a time |
| **Mix design submittals**, one PDF per project (including A Street Flats CMD-01) | The Phase 0 gate, and concrete cost-code suggestion for that project | Uploaded on `/projects/new` |
| **User emails** for Raz, Shant, the PEs, and you | Role assignment, reviewer and approver dropdowns | `/admin → Users`, or `migrations/004` |
| **Distribution list** emails | The end-of-day summary (Phase 2) | `/admin → Distribution list` |
| **`claude/buildertrend-invoice-upload-process.md`** — the full technical reference with BuilderTrend element ids and the JavaScript click snippets | Phase 3 only | Lives in the Claude project; needed when the uploader is built |

Only Linda's address is known, from SOP §1: `linda@ferrocretebuilders.com`.
Nothing else was guessed — a seeded row with an invented address is a user
who can never sign in, and it fails silently.

Everything above is data entry in `/admin` and `/projects/new`, not a code
change. That was the design goal of Phase 0.

---

## 6. Running the Phase 0 gate

1. Sign in as yourself. Promote to `admin` (step 2).
2. `/admin → Users`: add Linda, Raz, Shant, and the PEs with their roles.
3. `/admin → Cost codes`: confirm 236 codes loaded, and that the
   `3002 - Concrete Ready Mix` family carries element keywords. Those keywords
   are what the AI matches mix elements against; a code with none cannot be
   matched by element.
4. `/admin → Vendors`: confirm CalPortland maps to Catalina Pacific and White
   Cap defaults to `3015 - Hardware & Misc`.
5. `/projects/new`: enter A Street Flats, assign a PE, upload the CMD-01
   submittal. The yardage sheet is read and each mix arrives with a proposed
   cost code and a confidence pill. Correct anything wrong, then Save.
6. `/projects/[id]`: confirm the mix table, the onboarding badge, and that a
   new revision supersedes rather than replaces.
7. Sign in as a `pe` and confirm they can map a mix row's cost code but cannot
   edit project details or reach `/admin`.

---

## 7. Decisions taken in Phase 0

Worth knowing because they differ from the spec or resolve something it left
open:

- **Roles are an array**, not a single column as in the pay app, because §3
  requires Linda to hold two at once. `require_role` checks for an
  intersection.
- **New signups default to `viewer`**, not `pe`. A company email is not the
  same as permission to approve a bill.
- **Unmapped mix rows do not block onboarding.** §7.0 gates on "a mix design
  is confirmed", and a submittal often lists mixes a job never pours. A row
  with no cost code simply is never used to suggest a concrete code — which is
  the documented behavior — and the count is shown on both project screens.
- **Seven cost codes are seeded inactive**: the four `Bids` categories, both
  `Retention` accounts, and `Buildertrend Flat Rate`. They exist in
  BuilderTrend but must never receive a vendor bill.
- **The AI chooses from all active cost codes**, with the concrete families
  ordered first in the prompt. This was an explicit choice over restricting it
  to the concrete families.
- **`flag_reason` is split into `flag_code` and `flag_detail`.** §7.1 needs
  free error text, and `/flagged` needs to group by reason. One column could
  not do both.
- **`onboarded_at` is stamped after the mix rows land, not with the project
  insert.** There is no transaction across Supabase calls, so ordering is the
  only guarantee: a failure leaves an un-onboarded project, which is the safe
  state and is fixable from the detail screen.
- **Mix design revisions are versioned, never edited.** A superseded row is
  read-only, because invoices already approved were scored against it.

## 8. Known gaps to pick up later

- **Staged mix design PDFs are not garbage collected.** Uploading a submittal
  on `/projects/new` and then abandoning the form leaves a file under
  `mix-designs/_staging/`. Harmless but untidy; a sweep belongs in Phase 4
  alongside the other cleanup work.
- **The onboarding gate is duplicated**, in `onboarding_blockers()` on the
  server and in `/projects/new` on the client. The client copy exists so the
  reason Save is unavailable is visible before the click. They must be kept in
  sync by hand; the server copy is the guarantee.
- **Turning `has_concrete_supplier` on for an already-onboarded project with
  no mix design** leaves it onboarded but unable to suggest concrete codes.
  The API logs a warning and both project screens show the shortfall, but it
  does not un-onboard the project.
