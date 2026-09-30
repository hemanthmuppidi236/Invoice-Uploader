# Setup — Supabase, Google credentials, and deployment

Everything external the app needs, in the order it makes sense to do it.

There are **four separate credentials** here and they are easy to confuse,
because three of them come from the same Google Cloud project:

| # | What | Type | Used for | Needed by |
|---|---|---|---|---|
| 1 | Supabase project keys | URL + two API keys | Database, auth, file storage | Phase 0 |
| 2 | Google OAuth — **Web** client | Client ID + secret | People signing in to the app | Phase 0 |
| 3 | Google **service account** | JSON key file | Reading and filing invoices on Drive | Phase 1 |
| 4 | Google OAuth — **Desktop** client | Client ID + secret + refresh token | Sending mail as Ferrocrete | Phase 2 |

2 and 4 are both "OAuth client IDs" and are **not interchangeable**. The web
one has a redirect URL and is used by Supabase; the desktop one is used once
on your laptop to mint a refresh token. Creating one and using it for the
other is the most common way this setup goes wrong.

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

Start the app, sign in as yourself, then promote yourself in the Supabase SQL
editor — you land as `viewer` like everyone else:

```sql
update app_users
   set role = array['admin']::text[]
 where email = 'your.email@ferrocretebuilders.com';
```

Everyone else goes through `/admin → Users and roles` after that.

---

## 4. Drive access (service account) — Phase 1

A service account, not an OAuth client: the poll runs unattended on a cron, so
there is nobody to click a consent screen.

1. **IAM & Admin → Service Accounts → Create service account**
   - Name: `ferrocrete-invoice-intake`
   - No project roles needed — access is granted on the Drive folders, not in
     IAM.
2. Open it → **Keys → Add key → Create new key → JSON**. A file downloads.
3. **Share the Drive folders with the service account's email address**
   (it looks like `ferrocrete-invoice-intake@…iam.gserviceaccount.com`), with
   at least **Content manager** on the shared drive:
   - `Invoice Uploads/`
   - `White Cap/`
   - `BT Invoices/` (Phase 3 filing)

   **This is the step that gets missed.** Without it the Drive API returns an
   empty file list rather than a permission error, which looks exactly like
   "no new invoices" and gives you nothing to debug.
4. Get the folder IDs from each folder's URL — the part after `/folders/`.
5. Set the backend environment:

   ```bash
   # The whole JSON file contents, as one line
   GOOGLE_DRIVE_CREDENTIALS_JSON={"type":"service_account",...}
   DRIVE_FOLDER_INVOICE_UPLOADS=1AbC...
   DRIVE_FOLDER_WHITE_CAP=1DeF...
   DRIVE_FOLDER_BT_INVOICES=1GhI...
   ```

Delete the downloaded JSON from your Downloads folder once it is in Render.
`.gitignore` already blocks `service-account*.json`, but the safest copy is
the one that does not exist.

---

## 5. Sending mail (Desktop OAuth client) — Phase 2

A **second** OAuth client, separate from the web one in step 3.

1. **Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `Ferrocrete Invoice Processor — mail`
2. **Download JSON** and save it as `scripts/client_secret.json` in this repo.
   It is gitignored.
3. Mint the refresh token, once, on your laptop:

   ```bash
   cd backend && .venv/bin/pip install google-auth-oauthlib
   cd .. && backend/.venv/bin/python scripts/get_gmail_refresh_token.py
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
5. Delete `scripts/client_secret.json` once the refresh token is in Render.

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

Import the repo, root directory `frontend`. Three variables:

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
  "integrations": { "claude": true, "drive": true, "email": true, "agent_auth": true },
  "jobs_paused": false
}
```

A `false` there means that integration's environment variables are incomplete,
not that the service is down.

| Symptom | Almost always |
|---|---|
| Sign-in loops back to `/login` | The callback URL is missing from Supabase → Authentication → URL Configuration |
| `redirect_uri_mismatch` from Google | The Web client's redirect URI is not the Supabase `/auth/v1/callback` one |
| Poll finds nothing, no error | The Drive folders were never shared with the service account |
| Email worked, then stopped a week later | The consent screen is External + Testing; refresh tokens expire in 7 days |
| Every request 403s | Your `app_users` row still has `viewer`; promote it |
| 500s mentioning RLS | A `NEXT_PUBLIC_*` variable was given the `service_role` key, or the backend was given the `anon` key |
| `42P17: functions in index expression must be marked IMMUTABLE` | You are on a migration 001 from before 2026-09-30. Pull the latest — `created_at::DATE` on a TIMESTAMPTZ is STABLE and Postgres rejects it in an index. |
