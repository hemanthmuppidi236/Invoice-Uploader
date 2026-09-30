# Setup — Supabase, Google credentials, and deployment

Everything external the app needs, in the order it makes sense to do it.

There are **four separate credentials** here and they are easy to confuse,
because three of them come from the same Google Cloud project:

| # | What | Type | Used for | Needed by |
|---|---|---|---|---|
| 1 | Supabase project keys | URL + two API keys | Database, auth, file storage | Phase 0 |
| 2 | Google OAuth — **Web** client | Client ID + secret | People signing in to the app | Phase 0 |
| 3 | Drive access — **service account** JSON key, *or* an OAuth refresh token | either | Reading and filing invoices on Drive | Phase 1 |
| 4 | Google OAuth — **Desktop** client | Client ID + secret + refresh token | Sending mail as Ferrocrete | Phase 2 |

2 and 4 are both "OAuth client IDs" and are **not interchangeable**. The web
one has a redirect URL and is used by Supabase; the desktop one is used once
on your laptop to mint a refresh token. Creating one and using it for the
other is the most common way this setup goes wrong.

3 has two forms. If your Google org blocks service account creation — a
common Workspace policy — use the OAuth route in §4, which reuses the same
Desktop client as 4. No admin exception needed.

---

## 1. Supabase

### 1.1 Create the project

1. <https://supabase.com/dashboard> → **New project**.
2. Name it something like `ferrocrete-invoices`. **Do not reuse the pay app's
   project** — separate schema, separate storage, separate migration history.
3. Pick a strong database password and save it in your password manager. You
   will not need it for the app, but you will need it if you ever connect with
   `psql`.
4. Region: pick the one nearest San Diego (`us-west-1`).

### 1.2 Run the migrations

**SQL Editor** → paste and run each file, in order, one at a time. Read the
output of each before running the next.

| File | What it does |
|---|---|
| `migrations/001_initial_schema.sql` | 12 tables, indexes, triggers, RLS |
| `migrations/002_seed_cost_codes.sql` | 236 BuilderTrend cost codes |
| `migrations/003_seed_vendors.sql` | The SOP §6 vendor name mapping |
| `migrations/004_phase3_filing.sql` | Filing columns, and the unique index that stops one BuilderTrend bill being recorded against two invoices |

Migration 003 deliberately ends by raising an exception if the White Cap and
Cefali default cost codes did not resolve. If you see that error, 002 was not
applied first — run it, then re-run 003.

**If a migration fails partway through**, run `scripts/reset_schema.sql` to
drop everything migration 001 creates, then start again from 001. It touches
only this app's tables, so Supabase's own objects are untouched. Do not run it
once real invoices exist.

Sanity check afterwards — paste `scripts/verify_schema.sql` into the SQL
editor. Every row should read `OK`:

| what | expected |
|---|---|
| tables | 12 |
| cost codes seeded | 236 |
| cost codes active | 229 |
| vendors seeded | 6 |
| White Cap default code | `3015 - Hardware & Misc` |
| send-once index | 1 |
| signup trigger | 1 |
| storage buckets | `invoices, mix-designs` |

### 1.3 Create the storage buckets

**Storage** → **New bucket**, twice. Leave **Public bucket OFF** for both.

| Bucket | Holds |
|---|---|
| `invoices` | The invoice PDF pulled off Drive |
| `mix-designs` | Each project's mix design submittal |

Private matters. The app serves PDFs through short-lived signed URLs; a public
bucket would put every vendor invoice on a guessable URL.

### 1.4 Copy the keys

**Project Settings → API**:

| Dashboard label | Environment variable |
|---|---|
| Project URL | `SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_URL` |
| `anon` `public` | `SUPABASE_ANON_KEY` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` |
| `service_role` `secret` | `SUPABASE_SERVICE_ROLE_KEY` — **backend only** |

The `service_role` key bypasses every row-level security policy. It goes in
the backend environment and nowhere else. It must never appear in a
`NEXT_PUBLIC_*` variable, because everything prefixed that way is compiled
into the browser bundle.

---

## 2. Google Cloud project

Create **one** project and put all three Google credentials in it:
<https://console.cloud.google.com> → project picker → **New Project** →
`ferrocrete-invoices`.

If you can, create it **inside the ferrocretebuilders.com organization**
rather than under a personal account. That is what makes the "Internal"
consent screen available in the next step, which in turn is what makes the
refresh token permanent.

### 2.1 Enable the APIs

**APIs & Services → Library**, enable both:

- **Google Drive API** (Phase 1 — reading and filing invoices)
- **Gmail API** (Phase 2 — sending the digests)

### 2.2 OAuth consent screen — do this before either OAuth client

**APIs & Services → OAuth consent screen**.

**User type: Internal.** This is the single most consequential choice on this
page:

- **Internal** restricts sign-in to `@ferrocretebuilders.com` accounts at the
  Google level, which is the real enforcement behind the app's domain rule.
  It also means the Gmail refresh token never expires.
- **External** + "Testing" lets anyone with a Google account reach the consent
  screen, and **Google expires refresh tokens after 7 days**. The symptom is
  that email silently stops working a week after you set it up, with nothing
  in the app's logs to explain it.

If "Internal" is greyed out, the project is not inside the Workspace org. Move
it, or accept that you will be re-minting the Gmail refresh token weekly.

Fill in: app name `Ferrocrete Invoice Processor`, your address as both user
support and developer contact. Save.

---

## 3. Google sign-in for the app (Web OAuth client)

### 3.1 Create the client

**APIs & Services → Credentials → Create Credentials → OAuth client ID**

- Application type: **Web application**
- Name: `Ferrocrete Invoice Processor — web`
- **Authorized redirect URIs** — add exactly one, pointing at *Supabase*, not
  at your app:

  ```
  https://<your-project-ref>.supabase.co/auth/v1/callback
  ```

  The project ref is the subdomain of your Supabase project URL. This trips
  people up: the redirect goes to Supabase, which then redirects to your app.

Copy the **Client ID** and **Client secret**.

### 3.2 Wire it into Supabase

**Supabase → Authentication → Providers → Google**: enable it, paste the
client ID and secret, save.

### 3.3 Set the redirect URLs

**Supabase → Authentication → URL Configuration**:

- **Site URL**: `http://localhost:3000` while developing; your Vercel URL once
  deployed.
- **Redirect URLs** — add both, so the same Supabase project works locally and
  in production:

  ```
  http://localhost:3000/auth/callback
  https://your-app.vercel.app/auth/callback
  ```

A missing entry here is the usual cause of "signs in, then bounces back to
the login page".

### 3.4 Check the domain restriction

Three layers, all already in place:

1. The **Internal** consent screen stops non-Workspace accounts at Google.
2. The backend re-checks `ALLOWED_EMAIL_DOMAIN` on every request and returns
   403 — a misconfigured provider should not silently grant API access.
3. The signup trigger gives every new account the `viewer` role, so even a
   valid company account can read but write nothing until an admin grants a
   real role.

### 3.5 First sign-in

Start the app, sign in as yourself, then promote yourself — you land as
`viewer` like everyone else, and `/admin` is where roles are managed, so
somebody has to be promoted from outside the app once.

Edit the address at the top of **`scripts/bootstrap_admin.sql`** and run it in
the Supabase SQL editor. It refuses to apply if no row matches, which is the
difference that matters: a typo'd address otherwise leaves you a viewer with
nothing saying why.

Everyone else goes through `/admin → Users and roles` after that. Once you
have the whole team's addresses, `scripts/seed_users.sql.example` seeds them
in one go.

---

## 4. Drive access — Phase 1

Two ways to do this. Pick based on whether your Google org lets you create a
service account.

> **If "Create service account" is greyed out or errors**, your org enforces
> `iam.disableServiceAccountCreation`. That is common in Workspace. Skip to
> option B — it needs no admin involvement and is arguably a better fit here,
> because the account you use already has the folders rather than needing to
> be granted them.

### Option A — service account

Preferred where allowed: the identity belongs to the app, not to a person, so
nothing breaks when someone leaves.

1. **IAM & Admin → Service Accounts → Create service account**
   - Name: `ferrocrete-invoice-intake`
   - No project roles needed — access is granted on the Drive folders, not in
     IAM.
2. Open it → **Keys → Add key → Create new key → JSON**. A file downloads.
3. **Share the Drive folders with the service account's email address**
   (it looks like `ferrocrete-invoice-intake@…iam.gserviceaccount.com`), with
   at least **Content manager** on the shared drive:
   `Invoice Uploads/`, `White Cap/`, and `BT Invoices/`.

   **This is the step that gets missed.** Without it the Drive API returns an
   empty file list rather than a permission error, which looks exactly like
   "no new invoices" and gives you nothing to debug.
4. Set `GOOGLE_DRIVE_CREDENTIALS_JSON` to the whole key file as one line.

### Option B — OAuth user credentials

The same mechanism the app already uses for Gmail. You consent once as an
account that can see the folders; the cron reuses the refresh token.

1. You need the **Desktop OAuth client** from §5. If you have not made it yet,
   do that first — one client covers both Drive and Gmail.
2. Mint a Drive token:

   ```bash
   cd backend && .venv/bin/pip install google-auth-oauthlib
   cd .. && backend/.venv/bin/python scripts/get_google_refresh_token.py --scopes drive
   ```

   Or `--scopes both` to cover Drive and Gmail in one consent, if the same
   account should do both.

3. **Sign in as the account the app should act as.** Prefer a shared ops
   mailbox over a personal account: a refresh token belongs to whoever
   consented, and intake stops the day they leave or revoke it in
   <https://myaccount.google.com/permissions>.

4. Paste the printed values:

   ```bash
   GOOGLE_OAUTH_CLIENT_ID=...
   GOOGLE_OAUTH_CLIENT_SECRET=...
   DRIVE_OAUTH_REFRESH_TOKEN=...
   ```

   If that account is also your mail sender, the client id and secret are
   shared — the app falls back to the `GMAIL_OAUTH_` ones, so you can leave
   `GOOGLE_OAUTH_*` unset and just supply the Drive token.

**Two things specific to this option:**

- The `drive` scope is a **restricted** scope. On an **Internal** consent
  screen no app verification is needed. On an External one, Google requires a
  verification review that takes weeks — another reason §2.2 matters.
- Files the app writes in Phase 3 will be attributed to that account rather
  than to a service account. For filed invoice copies that is normal and
  arguably clearer.

### Either way: the folder IDs

Get them from the part of each folder's URL after `/folders/`:

```bash
DRIVE_FOLDER_INVOICE_UPLOADS=1AbC...
DRIVE_FOLDER_WHITE_CAP=1DeF...
DRIVE_FOLDER_BT_INVOICES=1GhI...     # Phase 3 filing
```

`GET /health` reports which mode is live:

```json
{ "integrations": { "drive": true }, "drive_auth": "oauth_user" }
```

`"drive_auth": null` with credentials set means a half-set pair — a refresh
token with no client secret, or the reverse.

Delete any downloaded key or client secret from your Downloads folder once the
values are in Render. `.gitignore` blocks `service-account*.json` and
`client_secret*.json`, but the safest copy is the one that does not exist.

---

## 5. Sending mail (Desktop OAuth client) — Phase 2

A **second** OAuth client, separate from the web one in step 3.

1. **Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `Ferrocrete Invoice Processor — mail`
2. **Download JSON** and save it as `scripts/client_secret.json` in this repo.
   This is the same client §4 option B uses.
   It is gitignored.
3. Mint the refresh token, once, on your laptop:

   ```bash
   cd backend && .venv/bin/pip install google-auth-oauthlib
   cd .. && backend/.venv/bin/python scripts/get_google_refresh_token.py --scopes gmail
   ```

   A browser opens. **Sign in as the mailbox the app should send as** — not
   necessarily your own account. Click Allow. The script prints three values.
4. Set the backend environment:

   ```bash
   EMAIL_PROVIDER=gmail_api
   GMAIL_OAUTH_CLIENT_ID=...
   GMAIL_OAUTH_CLIENT_SECRET=...
   GMAIL_OAUTH_REFRESH_TOKEN=...
   GMAIL_SENDER_EMAIL=noreply@ferrocretebuilders.com   # the account you just signed in as
   ADMIN_ALERT_EMAIL=you@ferrocretebuilders.com
   APP_URL=https://your-app.vercel.app                 # deep links inside the mail
   ```

   `GMAIL_SENDER_EMAIL` has to match the account that granted consent. Gmail
   rewrites the From header to that address regardless, so a mismatch means
   mail that appears to come from somewhere you did not expect.
5. Delete `scripts/client_secret.json` once both refresh tokens are in Render.

Test without waiting for the cron:

```bash
curl -X POST "$API_URL/jobs/email-daily?force=true" -H "X-Agent-Key: $AGENT_API_KEY"
```

---

## 6. The agent key

One shared secret covers both the Render cron jobs and, from Phase 3, the
Claude in Chrome upload session.

```bash
openssl rand -hex 32
```

Set it as `AGENT_API_KEY` in the backend environment. Leaving it unset denies
the agent, which is the safe default — the app will simply refuse those calls
rather than accepting anyone.

---

## 7. Deploy

### Backend — Render

New **Web Service**, root directory `backend`, Docker runtime. Set every
variable from `backend/.env.example`, plus:

```bash
APP_ENV=production
CORS_ORIGINS=https://your-app.vercel.app
```

### Frontend — Vercel

**Set the Root Directory to `frontend` before anything else.** Settings →
General → Root Directory. This repo holds two apps, and Vercel's detector
reaches `backend/` first: left at the repo root it tries to deploy the
FastAPI backend and the build dies with

```
Error: No FastAPI entrypoint found. Set "tool.vercel.entrypoint" in
pyproject.toml or define an entrypoint in one of: app.py, index.py, ...
```

That message names a Python problem, so it reads like the backend is
misconfigured. It is not — Vercel is simply pointed at the wrong half of the
repo. With the root directory set, the framework preset detects Next.js on
its own.

Then three variables:

```bash
NEXT_PUBLIC_SUPABASE_URL=https://<ref>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJ...
NEXT_PUBLIC_API_URL=https://your-service.onrender.com
```

Then go back and add the real Vercel URL to the backend's `CORS_ORIGINS` and
to Supabase's redirect URLs.

### Cron jobs — Render

| Job | Schedule (UTC) | Command |
|---|---|---|
| Drive poll | `*/15 * * * *` | `curl -fsS -X POST "$API_URL/jobs/poll-drive" -H "X-Agent-Key: $AGENT_API_KEY"` |
| Daily digest | `30 22 * * 1-5` | `curl -fsS -X POST "$API_URL/jobs/email-daily" -H "X-Agent-Key: $AGENT_API_KEY"` |
| End of day | `30 0 * * 2-6` | `curl -fsS -X POST "$API_URL/jobs/email-eod" -H "X-Agent-Key: $AGENT_API_KEY"` |

Render cron is UTC and does not follow daylight saving — see
`docs/PHASE_2.md` §3 for what that means in practice.

---

## 8. Verify

`GET /health` on the backend reports what is actually wired up, which is the
fastest way to find a missing key:

```json
{
  "status": "ok",
  "integrations": {
    "claude": true, "drive": true, "drive_auth": "oauth_user",
    "filing": true, "email": true, "agent_auth": true
  },
  "jobs_paused": false
}
```

A `false` there means that integration's environment variables are incomplete,
not that the service is down. `filing` is reported separately from `drive`
because intake needs only the intake folder: Drive credentials without
`DRIVE_FOLDER_BT_INVOICES` look healthy right up to the first successful
BuilderTrend upload.

| Symptom | Almost always |
|---|---|
| Sign-in loops back to `/login` | The callback URL is missing from Supabase → Authentication → URL Configuration |
| `Unsupported provider: provider is not enabled` | Google is not enabled in Supabase → Authentication → Providers. The toggle does not persist until you press Save, and will not save with the client ID or secret blank |
| `Cannot convert argument to a ByteString ... value of 8232` | A Supabase key was pasted with an invisible character (8232 is U+2028 LINE SEPARATOR). Both apps now strip these, so this only bites a deployment built before that — re-paste the key and redeploy |
| `redirect_uri_mismatch` from Google | The Web client's redirect URI is not the Supabase `/auth/v1/callback` one |
| Poll finds nothing, no error | Service account mode: the folders were never shared with it. OAuth mode: the account you consented as cannot see them. |
| Cannot create a service account | Org policy `iam.disableServiceAccountCreation`. Use §4 option B; no admin exception needed. |
| Google demands app verification | The consent screen is External and `drive` is a restricted scope. Switch to Internal (§2.2). |
| Email worked, then stopped a week later | The consent screen is External + Testing; refresh tokens expire in 7 days |
| Every request 403s | Your `app_users` row still has `viewer`; run `scripts/bootstrap_admin.sql` |
| Vercel build fails with `No FastAPI entrypoint found` | Root Directory is not set to `frontend`, so Vercel is building the backend. See §7 |
| Vercel page is unavailable, but the build "succeeded" | Usually the same cause: Vercel built something that is not the Next.js app. Check the build log names Next.js. If it does, the three `NEXT_PUBLIC_*` variables are missing — the middleware builds a Supabase client on every request and throws without them |
| Added the `NEXT_PUBLIC_*` variables and nothing changed | They are inlined at build time. Saving them does nothing on its own; **redeploy** |
| The app loads but every API call fails | `NEXT_PUBLIC_API_URL` is wrong, or the backend's `CORS_ORIGINS` does not list the exact Vercel URL including `https://` |
| 500s mentioning RLS | A `NEXT_PUBLIC_*` variable was given the `service_role` key, or the backend was given the `anon` key |
| Render deploy exits 1 with `ValidationError ... Field required` | A required backend variable is unset. The startup error names every missing one and where to find it — read the line above the traceback, not the traceback |
| Supabase shows `publishable` / `secret` keys, not `anon` / `service_role` | Newer dashboards moved the JWT keys under **Legacy API keys**. Use those; they start `eyJ`, and the frontend and backend must carry the same `anon` value |
| `42P17: functions in index expression must be marked IMMUTABLE` | You are on a migration 001 from before 2026-09-30. Pull the latest — `created_at::DATE` on a TIMESTAMPTZ is STABLE and Postgres rejects it in an index. |
